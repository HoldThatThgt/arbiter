"""Content-addressed fact store + query engine, absorbed from cipher-2 (M4).

Near-verbatim import of cipher-2's `storage/` namespace, adapted at two seams: the
`cipher2.common.JSONValue` type and the `cipher2.tools.log` sink are replaced by the
local `._common` shim (see docs/proposals/m4-facts-absorption.md). Stdlib + sqlite3 +
hashlib only — passes the engine stdlib-import gate.
"""
