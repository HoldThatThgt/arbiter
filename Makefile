# Go dependencies must be vendored when introduced. This scaffold has no external deps.

PYTHON ?= python3

.PHONY: build test test-go test-py fmt-check

build:
	go build ./cmd/arbiter

test: test-go test-py

test-go:
	go vet ./...
	go test -race ./...

test-py:
	PYTHONPATH=engine $(PYTHON) -m unittest discover -s engine/tests

fmt-check:
	@test -z "$$(gofmt -l .)" || (gofmt -l . && exit 1)
