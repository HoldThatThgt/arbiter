# Go dependencies must be vendored when introduced. This scaffold has no external deps.

.PHONY: build test fmt-check

build:
	go build ./cmd/arbiter

test:
	go vet ./...
	go test -race ./...

fmt-check:
	@test -z "$$(gofmt -l .)" || (gofmt -l . && exit 1)
