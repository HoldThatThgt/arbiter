"""FACT and relative file snapshot store for cipher-2."""

from __future__ import annotations

import sys
import types

from .constants import *
from .models import *
from .views import *
from .search import *
from .store import FileFactStore, open_fact_store

_COMPAT_MODULES = (
    "constants",
    "models",
    "utils",
    "search",
    "views",
    "serialization",
    "read_index",
    "snapshot_reader",
    "snapshot_writer",
    "store_events",
    "store",
)


class _StorageCompatModule(types.ModuleType):
    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        package = __name__
        for module_name in _COMPAT_MODULES:
            module = sys.modules.get(f"{package}.{module_name}")
            if module is not None and hasattr(module, name):
                types.ModuleType.__setattr__(module, name, value)


sys.modules[__name__].__class__ = _StorageCompatModule

__all__ = [
    "FactRecord",
    "FactRelative",
    "FactView",
    "EncodedFactLine",
    "EncodedRelativeLine",
    "FileFactStore",
    "RelationSearchAnchorCandidate",
    "RelationSearchMatch",
    "RelationSearchMatchedRelation",
    "RelationSearchPathNode",
    "RelationSearchQuery",
    "RelationSearchResult",
    "RelativeCondition",
    "SourceInventoryEntry",
    "StorageError",
    "StorageManifest",
    "StorageStats",
    "StoredFactLine",
    "StoredRelativeLine",
    "StoredSourceInventoryLine",
    "TemporaryOverlay",
    "open_fact_store",
    "parse_relation_search_query",
]
