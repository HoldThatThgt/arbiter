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


__all__ = ["arbiter_dir", "config_path", "facts_dir", "load_config"]
