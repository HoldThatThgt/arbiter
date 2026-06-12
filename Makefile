# Go dependencies must be vendored when introduced. This scaffold has no external deps.

PYTHON ?= python3
PREFIX ?= $(HOME)/.local

.PHONY: build install test test-go test-py fmt-check transcripts

build:
	go build ./cmd/arbiter

# The ONE install command (ADR-0011): the binary embeds the Python engine
# (gdb-mcp + perf-mcp included), so installing this single file is the whole
# product. `arbiter init` materializes the engine per-repo as needed.
install: build
	install -d $(PREFIX)/bin
	install -m 0755 arbiter $(PREFIX)/bin/arbiter
	@echo "installed: $(PREFIX)/bin/arbiter"
	@case ":$$PATH:" in *":$(PREFIX)/bin:"*) ;; *) echo "note: add $(PREFIX)/bin to PATH";; esac
	@echo "next: cd <your repo> && arbiter init"

test: test-go test-py

test-go:
	go vet ./...
	go test -race ./...

test-py:
	PYTHONPATH=engine $(PYTHON) -m unittest discover -s engine/tests

fmt-check:
	@test -z "$$(gofmt -l cmd internal engine)" || (gofmt -l cmd internal engine && exit 1)

transcripts:
	PYTHONPATH=engine $(PYTHON) engine/tests/write_transcripts.py
