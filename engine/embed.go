package enginebundle

import "embed"

// FS contains the Python engine package for opt-in embedded-engine deployments.
// The glob is depth-explicit (go:embed has no recursive '**'); the deepest package is
// arbiter_engine/facts/extractor/code/*.py. The '*' wildcard matches '_'-prefixed files
// (__init__.py, _shim.py, _common.py), so the absorbed store/extractor are included.
//
//go:embed arbiter_engine/*.py arbiter_engine/*/*.py arbiter_engine/*/*/*.py arbiter_engine/*/*/*/*.py
var FS embed.FS
