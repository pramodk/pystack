"""Bind-mount the target library over the mapped path, then call the native sleeper."""

import ctypes
import os
import subprocess
import sys

TARGET_SYMBOL = "pystack_target_process_root_symbol"

target_dir, mapped_dir, library_name = sys.argv[1:4]
subprocess.run(["mount", "--bind", target_dir, mapped_dir], check=True)
library = ctypes.CDLL(os.path.join(mapped_dir, library_name))
getattr(library, TARGET_SYMBOL)()
