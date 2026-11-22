"""``python -m helper_plus`` 入口。"""

from __future__ import annotations

import sys

from .ui.app import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
