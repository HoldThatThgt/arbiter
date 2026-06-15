"""Incremental-test support for the migrated cipher-2 suites.

cipher-2's incremental tests call ``load_config(target, overrides={"incremental": {...}})``
and get a full CipherConfig. Arbiter splits config, so the coordinator only needs the live
``facts.incremental`` knobs — this shim returns an ``IncrementalConfig`` with any overrides
applied. Kept separate from toolchain_helpers (which serves the extractor/initializer tests)
so the two suites don't contend over one module.
"""

import json
from dataclasses import replace
from pathlib import Path
from typing import Optional

from arbiter_engine.config import IncrementalConfig
from arbiter_engine.facts import incremental, relocation


def load_config(target=None, *, overrides: Optional[dict] = None, observe: bool = False) -> IncrementalConfig:
    config = IncrementalConfig()
    section = (overrides or {}).get("incremental") if overrides else None
    if section:
        config = replace(config, **{key: value for key, value in section.items() if hasattr(config, key)})
    return config


def publish_overlay(repo, overlay, *, base_snapshot_id: Optional[str] = None) -> None:
    """Write a TemporaryOverlay to disk in the layout `load_active_overlay` reads.

    The inverse of `incremental.load_active_overlay` — lets a migrated test publish an arbitrary
    overlay (instead of cipher-2's in-memory fact_view_provider injection) so the cwd-bound rpc
    reader merges it via `store.open_view(load_active_overlay(...))`.
    """
    repo = Path(repo)
    overlay_dir = relocation.facts_dir(repo) / "run" / "incremental" / "overlays" / overlay.overlay_id
    overlay_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(overlay_dir / "facts.upsert.jsonl", ({"payload": fact.to_json()} for fact in overlay.fact_upserts))
    _write_jsonl(overlay_dir / "facts.tombstone.jsonl", ({"payload": {"source_id": sid}} for sid in sorted(overlay.source_tombstones)))
    _write_jsonl(overlay_dir / "relatives.upsert.jsonl", ({"payload": rel.to_json()} for rel in overlay.relative_upserts))
    _write_jsonl(overlay_dir / "relatives.tombstone.jsonl", ({"relative_id": rid} for rid in sorted(overlay.relative_tombstones)))

    pointer = incremental.overlay_pointer_path(repo)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(
        json.dumps(
            {"base_snapshot_id": base_snapshot_id, "overlay_id": overlay.overlay_id, "view_state": "overlay"},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    status = incremental.IncrementalStatus(
        "overlay",
        base_snapshot_id,
        overlay_id=overlay.overlay_id,
        overlay_fact_count=len(overlay.fact_upserts),
        overlay_relative_count=len(overlay.relative_upserts),
    )
    state = relocation.facts_dir(repo) / "run" / "incremental" / "state.json"
    state.write_text(json.dumps(status.to_json(), sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def _write_jsonl(path, rows) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")
