# =============================================================================
# orion/__main__.py
# -----------------------------------------------------------------------------
# Responsible for: Making `python -m orion ...` an entry point identical to the
#                  installed `orion` console script.
# Role in project: The portable, OS-neutral way to invoke Orion. The installed
#                  console script lives at a venv path that differs per platform
#                  (`.venv/bin/orion` on macOS/Linux, `.venv\Scripts\orion.exe`
#                  on native Windows), so the docs lead with `python -m orion`,
#                  which resolves the same way everywhere. This module is what
#                  makes that form work: Python runs it when given `-m orion`.
# Assumptions: `orion.cli.main` returns a process exit code (0 success, non-zero
#              failure); we forward it to the interpreter via sys.exit.
# =============================================================================

import sys

from orion.cli import main

# `python -m orion` executes this module as "__main__". Delegate straight to the
# same main() the console script calls, so both invocations share one code path
# (no duplicated argument handling) and exit with the code main() returns.
if __name__ == "__main__":
    sys.exit(main())
