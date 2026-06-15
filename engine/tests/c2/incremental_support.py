"""Incremental-test support for the migrated cipher-2 suites.

cipher-2's incremental tests call ``load_config(target, overrides={"incremental": {...}})``
and get a full CipherConfig. Arbiter splits config, so the coordinator only needs the live
``facts.incremental`` knobs — this shim returns an ``IncrementalConfig`` with any overrides
applied. Kept separate from toolchain_helpers (which serves the extractor/initializer tests)
so the two suites don't contend over one module.
"""

from dataclasses import replace
from typing import Optional

from arbiter_engine.config import IncrementalConfig


def load_config(target=None, *, overrides: Optional[dict] = None, observe: bool = False) -> IncrementalConfig:
    config = IncrementalConfig()
    section = (overrides or {}).get("incremental") if overrides else None
    if section:
        config = replace(config, **{key: value for key, value in section.items() if hasattr(config, key)})
    return config
