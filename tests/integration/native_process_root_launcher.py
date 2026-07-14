"""Create the private bind mount, then exec the Python target program."""

import os
import subprocess
import sys


target_dir, mapped_dir, ready_fifo, target_program, library_name = sys.argv[1:6]

subprocess.run(["mount", "--bind", target_dir, mapped_dir], check=True)
mapped_library = os.path.join(mapped_dir, library_name)
os.execv(
    sys.executable,
    [sys.executable, "-S", target_program, ready_fifo, mapped_library],
)
