"""讓 `python -m arcx_auto` 可以直接執行。"""

import sys

from arcx_auto.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
