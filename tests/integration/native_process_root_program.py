"""Load the target library and call the native sleeper symbol."""

import ctypes
import os
import sys


TARGET_SYMBOL = "pystack_target_process_root_symbol"

ready_fifo, library_path = sys.argv[1:3]
library = ctypes.CDLL(library_path)
symbol = getattr(library, TARGET_SYMBOL)
symbol.argtypes = (ctypes.c_char_p,)
symbol.restype = None
symbol(os.fsencode(ready_fifo))
