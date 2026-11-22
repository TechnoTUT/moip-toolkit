# moip-toolkit build
#
#   make            build the engine (Rust) and the server (Go) into bin/
#   make frontend   build the web UI (needs Node.js)
#   make test       run the Rust and Go tests
#
# The NDI SDK is not needed to build: libndi.so.6 is loaded at run time.

CARGO ?= cargo
GO    ?= go
VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
NPM   ?= npm

.PHONY: all engine server frontend test clean submodules

all: engine server

# engine/ is a git submodule (TechnoTUT/moip-engine).
submodules:
	@test -f engine/Cargo.toml || git submodule update --init

engine: submodules
	$(CARGO) build --release --manifest-path engine/Cargo.toml
	mkdir -p bin
	cp engine/target/release/moip-engine bin/moip-engine

server:
	mkdir -p bin
	cd server && $(GO) build -trimpath -ldflags "-X main.version=$(VERSION)" -o ../bin/moip ./cmd/moip

frontend:
	cd frontend && $(NPM) ci && $(NPM) run build

test: submodules
	$(CARGO) test --manifest-path engine/Cargo.toml
	cd server && $(GO) vet ./... && $(GO) test ./...

clean:
	rm -rf bin
	$(CARGO) clean --manifest-path engine/Cargo.toml
