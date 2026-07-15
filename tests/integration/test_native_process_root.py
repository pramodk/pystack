from __future__ import annotations

import contextlib
import os
import select
import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Generator
from typing import NoReturn

import pytest

from pystack.engine import NativeReportingMode
from pystack.engine import get_process_threads
from tests.utils import TIMEOUT

# Same filename, different symbols, so a host-path lookup is visible in frames.
LIBRARY_NAME = "libpystack_process_root_test.so"
TARGET_SYMBOL = "pystack_target_process_root_symbol"
HOST_DECOY_SYMBOL = "pystack_host_decoy_symbol"
READY_MESSAGE = b"ready"

# Focused local/CI verification can turn namespace setup skips into failures.
REQUIRE_PROCESS_ROOT_TEST = os.environ.get("PYSTACK_REQUIRE_PROCESS_ROOT_TEST") == "1"

# Keep the child program beside the other integration fixtures.
TEST_NATIVE_PROCESS_ROOT_PROGRAM = (
    Path(__file__).parent / "native_process_root_program.py"
)


def skip_or_fail(reason: str) -> NoReturn:
    """Skip unless this test was explicitly requested as required."""
    if REQUIRE_PROCESS_ROOT_TEST:
        pytest.fail(reason)
    pytest.skip(reason)


def compiler_command() -> list[str] | None:
    """Return the C compiler command, honoring CC when it is set."""
    compiler = os.environ.get("CC")
    if compiler:
        return shlex.split(compiler)

    compiler = shutil.which("cc") or shutil.which("gcc")
    if compiler is None:
        return None
    return [compiler]


def compile_native_sleeper(compiler: list[str], output: Path, symbol: str) -> None:
    """Build a shared library that signals readiness from inside symbol."""
    source = output.with_suffix(".c")
    source.write_text(textwrap.dedent(f"""
            #include <unistd.h>

            __attribute__((noinline)) void
            {symbol}(void)
            {{
                write(STDOUT_FILENO, "{READY_MESSAGE.decode()}", {len(READY_MESSAGE)});
                sleep(1000);
            }}
            """))
    subprocess.run(
        [
            *compiler,
            "-g",
            "-O0",
            "-fno-omit-frame-pointer",
            "-fPIC",
            "-shared",
            "-o",
            str(output),
            str(source),
        ],
        check=True,
    )


def available_mount_namespace_command() -> list[str] | None:
    """Return an unshare command that can create the test namespace."""
    unshare = shutil.which("unshare")
    if unshare is None:
        return None

    if os.geteuid() == 0:
        command = [unshare, "--mount", "--propagation", "private"]
    else:
        command = [
            unshare,
            "--user",
            "--map-root-user",
            "--mount",
            "--propagation",
            "private",
        ]

    if (
        subprocess.run(
            [*command, "true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    ):
        return command
    return None


def wait_for_target_ready(process: subprocess.Popen) -> None:
    """Wait for the readiness message written from inside the native symbol."""
    assert process.stdout is not None
    stdout_fd = process.stdout.fileno()
    readable, _, _ = select.select([stdout_fd], [], [], TIMEOUT)
    if not readable:
        process.kill()
        pytest.fail("timed out waiting for target process")
    if os.read(stdout_fd, len(READY_MESSAGE)) == READY_MESSAGE:
        return

    # EOF on stdout: the target died before it was ready.
    _, stderr = process.communicate()
    message = stderr.strip()
    if "Operation not permitted" in message or "permission denied" in message.lower():
        skip_or_fail(f"mount namespace setup is not permitted: {message}")
    pytest.fail(f"target process exited before it was ready: {message}")


@contextlib.contextmanager
def spawn_namespaced_target(
    unshare_command: list[str],
    target_dir: Path,
    mapped_dir: Path,
) -> Generator[subprocess.Popen, None, None]:
    """Run the target after bind-mounting target_dir over mapped_dir."""
    with subprocess.Popen(
        [
            *unshare_command,
            sys.executable,
            "-S",
            str(TEST_NATIVE_PROCESS_ROOT_PROGRAM),
            str(target_dir),
            str(mapped_dir),
            LIBRARY_NAME,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        wait_for_target_ready(process)
        try:
            yield process
        finally:
            process.kill()
            process.wait(timeout=TIMEOUT)


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux procfs")
def test_native_symbols_use_target_process_root(tmp_path: Path) -> None:
    """Test native unwinding when the target process sees a different filesystem.

    The host has a decoy shared library at the mapped path.  Inside the target
    mount namespace, that same path is bind-mounted to the real target library.
    PyStack should resolve the mapped file through /proc/<pid>/root and report
    symbols from the target library, not from the host decoy.
    """

    # GIVEN: the host and target process see different libraries at mapped_library.
    compiler = compiler_command()
    if compiler is None:
        skip_or_fail("a C compiler is required to build the test shared libraries")
    if shutil.which("mount") is None:
        skip_or_fail("mount is required to set up the private mount namespace")

    unshare_command = available_mount_namespace_command()
    if unshare_command is None:
        skip_or_fail("user and mount namespaces are not available")

    target_dir = tmp_path / "target"
    mapped_dir = tmp_path / "mapped"
    target_dir.mkdir()
    mapped_dir.mkdir()

    target_library = target_dir / LIBRARY_NAME
    mapped_library = mapped_dir / LIBRARY_NAME
    compile_native_sleeper(compiler, target_library, TARGET_SYMBOL)
    compile_native_sleeper(compiler, mapped_library, HOST_DECOY_SYMBOL)

    with spawn_namespaced_target(unshare_command, target_dir, mapped_dir) as process:
        process_root_library = Path(f"/proc/{process.pid}/root") / str(
            mapped_library
        ).lstrip(os.sep)

        # Verify the host path and target-root path resolve to different files.
        assert mapped_library.exists()
        assert process_root_library.exists()
        assert os.stat(mapped_library).st_ino != os.stat(process_root_library).st_ino

        # WHEN: PyStack collects native frames from the target process.
        threads = list(
            get_process_threads(
                process.pid,
                native_mode=NativeReportingMode.PYTHON,
                stop_process=True,
            )
        )

        # THEN: native symbols come from the target library, not the host decoy.
        symbols = {frame.symbol for thread in threads for frame in thread.native_frames}
        assert TARGET_SYMBOL in symbols
        assert HOST_DECOY_SYMBOL not in symbols
