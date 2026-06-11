"""Package entry point for ``python -m cipher2``."""

from __future__ import annotations

import sys

from cipher2.cli import main


if __name__ == "__main__":
    sys.exit(main())
