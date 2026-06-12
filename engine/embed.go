package enginebundle

import "embed"

// FS contains the Python engine package for opt-in embedded-engine deployments.
//
//go:embed arbiter_engine/*.py arbiter_engine/*/*.py
var FS embed.FS
