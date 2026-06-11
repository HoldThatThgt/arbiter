"""Clang AST-backed C FACT and FactRelative extractor for cipher-2 v1."""

from __future__ import annotations

import sys
import types

from .constants import *
from .models import *
from .mapper_utils import *
from .toolchain import *
from .compile_db import *
from .ast_backend import *
from .mapper import *
from .direct_calls import *
from .streaming_segments import *
from .streaming import *
from .extractor import *
from . import ast_backend as _ast_backend_module

_COMPAT_MODULES = (
    "constants",
    "models",
    "mapper_utils",
    "toolchain",
    "compile_db",
    "ast_backend",
    "mapper",
    "direct_calls",
    "streaming_segments",
    "streaming",
    "extractor",
)


class _CodeExtractorCompatModule(types.ModuleType):
    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        package = __name__
        for module_name in _COMPAT_MODULES:
            module = sys.modules.get(f"{package}.{module_name}")
            if module is not None and hasattr(module, name):
                types.ModuleType.__setattr__(module, name, value)


sys.modules[__name__].__class__ = _CodeExtractorCompatModule


def _install_json_test_libclang_backend() -> None:
    _ast_backend_module._install_json_test_libclang_backend()
    globals()["_TEST_AST_BACKEND_FACTORY"] = _ast_backend_module._TEST_AST_BACKEND_FACTORY


def _clear_test_libclang_backend() -> None:
    _ast_backend_module._clear_test_libclang_backend()
    globals()["_TEST_AST_BACKEND_FACTORY"] = _ast_backend_module._TEST_AST_BACKEND_FACTORY

__all__ = [
    "CodeFact",
    "CodeFactExtractor",
    "DirectCallEvidence",
    "ExtractionResult",
    "ToolchainProbeResult",
]
