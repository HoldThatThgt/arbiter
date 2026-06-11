"""Command-line entrypoint for the Arbiter engine scaffold."""

from __future__ import annotations

import argparse
from typing import Optional, Sequence

from . import __version__


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="arbiter-engine")
    parser.add_argument("--version", action="store_true", help="print the engine version")
    args = parser.parse_args(argv)

    if args.version:
        print(f"arbiter-engine {__version__}")
        return 0

    parser.print_usage()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
