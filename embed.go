// Package arbiter exposes build-time embedded assets shared across the binary.
package arbiter

import "embed"

// EngineFS carries the Python engine tree (ADR-0011): delivering the binary
// delivers the engine. deploy materializes it into repo-local .arbiter/engine/
// when no installed arbiter-engine package resolves, so install is one
// artifact and init is one command. Only *.py files are ever unpacked; the
// all: prefix is required because package files like __init__.py start with
// an underscore, which plain go:embed patterns exclude.
//
//go:embed all:engine/arbiter_engine
var EngineFS embed.FS
