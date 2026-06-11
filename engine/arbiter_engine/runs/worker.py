"""Bounded async run worker entrypoint."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import _run_worker_guarded


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        print("usage: worker <db_path> <run_id> <spec_json>", file=sys.stderr)
        return 2
    db_path = Path(args[0])
    run_id = args[1]
    spec = json.loads(args[2])
    _run_worker_guarded(db_path, run_id, spec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
