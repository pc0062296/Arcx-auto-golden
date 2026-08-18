"""Allow `python -m arcx_auto`."""

import sys

from arcx_auto.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
