"""cipher-2 tests migrated as acceptance for the M4 facts absorption.

Each module here is cipher-2's own test, copied near-verbatim with the import surface
rewritten from `cipher2.*` to the absorbed `arbiter_engine.facts.*` packages (see
docs/proposals/m4-test-migration-map.md). Our port must pass them. Extractor tests added
in a later phase install the JSON test libclang backend here so they run hermetically.
"""
