import os
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from pystack.engine import NativeReportingMode
from pystack.engine import get_process_threads


LIBRARY_NAME = "libpystack_process_root_test.so"
REQUIRE_PROCESS_ROOT_TEST = os.environ.get("PYSTACK_REQUIRE_PROCESS_ROOT_TEST") == "1"


def skip_or_fail(reason: str) -> None:
    if REQUIRE_PROCESS_ROOT_TEST:
        pytest.fail(reason)
    pytest.skip(reason)


def compiler_command() -> list[str] | None:
    compiler = os.environ.get("CC")
    if compiler:
        return shlex.split(compiler)

    compiler = shutil.which("cc") or shutil.which("gcc")
    if compiler is None:
        return None
    return [compiler]


def compile_shared_library(
    compiler: list[str],
    source: Path,
    output: Path,
    symbol: str,
) -> None:
    source.write_text(
        textwrap.dedent(
            f"""
            #include <fcntl.h>
            #include <unistd.h>

            __attribute__((noinline)) void
            {symbol}(const char* ready_path)
            {{
                int fd = open(ready_path, O_WRONLY | O_CREAT | O_TRUNC, 0666);
                if (fd >= 0) {{
                    write(fd, "ready", 5);
                    close(fd);
                }}
                sleep(1000);
            }}
            """
        )
    )
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


def unshare_mount_namespace_command() -> list[str] | None:
    unshare = shutil.which("unshare")
    if unshare is None:
        return None

    candidates = []
    if os.geteuid() == 0:
        candidates.append([unshare, "--mount", "--propagation", "private"])
    candidates.append(
        [
            unshare,
            "--user",
            "--map-root-user",
            "--mount",
            "--propagation",
            "private",
        ]
    )

    for command in candidates:
        if (
            subprocess.run(
                [
                    *command,
                    "true",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        ):
            return command

    return None


def wait_until_ready(process: subprocess.Popen, ready_file: Path) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if ready_file.exists():
            return
        if process.poll() is not None:
            _, stderr = process.communicate()
            message = stderr.strip()
            if "Operation not permitted" in message or "permission denied" in message.lower():
                skip_or_fail(f"mount namespace setup is not permitted: {message}")
            pytest.fail(f"target process exited before it was ready: {message}")
        time.sleep(0.1)

    process.terminate()
    pytest.fail("timed out waiting for target process")


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux procfs")
def test_native_symbols_use_target_process_root(tmp_path: Path) -> None:
    compiler = compiler_command()
    if compiler is None:
        skip_or_fail("a C compiler is required to build the test shared libraries")
    if shutil.which("mount") is None:
        skip_or_fail("mount is required to set up the private mount namespace")

    unshare_command = unshare_mount_namespace_command()
    if unshare_command is None:
        skip_or_fail("user and mount namespaces are not available")

    target_dir = tmp_path / "target"
    host_dir = tmp_path / "mapped"
    target_dir.mkdir()
    host_dir.mkdir()

    target_library = target_dir / LIBRARY_NAME
    host_library = host_dir / LIBRARY_NAME
    mapped_library = host_library

    compile_shared_library(
        compiler,
        tmp_path / "target.c",
        target_library,
        "pystack_target_process_root_symbol",
    )
    compile_shared_library(
        compiler,
        tmp_path / "host.c",
        host_library,
        "pystack_host_decoy_symbol",
    )

    target_program = tmp_path / "target_program.py"
    target_program.write_text(
        textwrap.dedent(
            """
            import ctypes
            import os
            import sys
            from pathlib import Path

            library = ctypes.CDLL(sys.argv[2])
            symbol = library.pystack_target_process_root_symbol
            symbol.argtypes = (ctypes.c_char_p,)
            symbol.restype = None
            symbol(os.fsencode(Path(sys.argv[1])))
            """
        )
    )

    namespace_launcher = tmp_path / "namespace_launcher.py"
    namespace_launcher.write_text(
        textwrap.dedent(
            """
            import os
            import subprocess
            import sys
            from pathlib import Path

            target_dir = sys.argv[1]
            host_dir = sys.argv[2]
            ready_file = sys.argv[3]
            target_program = sys.argv[4]
            library_name = sys.argv[5]

            subprocess.run(["mount", "--bind", target_dir, host_dir], check=True)
            mapped_library = str(Path(host_dir) / library_name)
            os.execv(
                sys.executable,
                [sys.executable, "-S", target_program, ready_file, mapped_library],
            )
            """
        )
    )

    ready_file = tmp_path / "ready"
    process = subprocess.Popen(
        [
            *unshare_command,
            sys.executable,
            "-S",
            str(namespace_launcher),
            str(target_dir),
            str(host_dir),
            str(ready_file),
            str(target_program),
            LIBRARY_NAME,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        wait_until_ready(process, ready_file)
        process_root_library = Path(f"/proc/{process.pid}/root") / str(
            mapped_library
        ).lstrip(os.sep)

        assert mapped_library.exists()
        assert process_root_library.exists()
        assert os.stat(mapped_library).st_ino != os.stat(process_root_library).st_ino

        threads = list(
            get_process_threads(
                process.pid,
                native_mode=NativeReportingMode.PYTHON,
                stop_process=True,
            )
        )

        symbols = {frame.symbol for thread in threads for frame in thread.native_frames}
        assert "pystack_target_process_root_symbol" in symbols
        assert "pystack_host_decoy_symbol" not in symbols
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
