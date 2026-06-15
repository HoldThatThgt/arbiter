"""cipher-2 tests migrated as acceptance for the M4 facts absorption.

Each module here is cipher-2's own test, copied near-verbatim with the import surface
rewritten from `cipher2.*` to the absorbed `arbiter_engine.facts.*` packages (see
docs/proposals/m4-test-migration-map.md). Our port must pass them.

Importing this package installs the JSON test libclang backend (the unittest equivalent of
cipher-2's tests/__init__.py side-effect), so extractor-driven tests run hermetically against a
JSON-AST oracle instead of a real libclang. Tests needing a genuine toolchain call
`_clear_test_libclang_backend()` themselves.
"""

from arbiter_engine.facts.extractor import code as _code_extractor

_code_extractor._install_json_test_libclang_backend()
