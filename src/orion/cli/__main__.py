# =============================================================================
# cli/__main__.py
# -----------------------------------------------------------------------------
# Responsible for: Making `python -m orion.cli ...` run the CLI, exactly like the
#                  `orion` console script and `python -m orion`.
# Role in project: Without this, that invocation would merely IMPORT the package and
#                  exit 0 WITHOUT running main() — a silent no-op. That footgun masked
#                  a real failure once (`python -m orion.cli <cmd>` returned 0 while
#                  pushing nothing, so the live dashboard stayed empty). Forwarding
#                  main()'s exit code here means every reasonable invocation actually runs.
# =============================================================================
import sys

from orion.cli import main

if __name__ == "__main__":
    sys.exit(main())
