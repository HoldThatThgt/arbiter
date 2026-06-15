"""Arbiter-specific facts relocation helpers."""

from __future__ import annotations

from pathlib import Path

from arbiter_engine import config


def arbiter_dir(repo: Path) -> Path:
    return Path(repo) / ".arbiter"


def facts_dir(repo: Path) -> Path:
    return arbiter_dir(repo) / "facts"


def config_path(repo: Path) -> Path:
    return arbiter_dir(repo) / "config.yml"


def load_config(repo: Path) -> config.FactsConfig:
    return config.load_config(config_path(repo)).facts


def persisted_compile_db_path(repo: Path) -> Path:
    """Stable location of the last published build's compile-db.

    The build pipeline persists the compile-db here on publish so the incremental
    coordinator's reconcile can re-extract dirty sources with the build's flags — the
    recipe's ``compile_db.path`` is not visible to the facts layer.
    """
    return facts_dir(repo) / "run" / "compile-db.json"


__all__ = ["arbiter_dir", "config_path", "facts_dir", "load_config", "persisted_compile_db_path"]
