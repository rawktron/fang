#!/usr/bin/env bash
# End-to-end hello world test for fang-runtime.
# Requires: FANG_PYTHON_VERSION env var (e.g. 3.12.3+20240415)
# Uses: the python-build-standalone install_only tarball already in
#       ~/.fang/cpython-cache/ to compile the .py source.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="$(cd "$SCRIPT_DIR/../../.." && pwd)"

PYTHON_VERSION="${FANG_PYTHON_VERSION:-3.12.3+20240415}"
CACHE_DIR="${FANG_CPYTHON_CACHE:-$HOME/.fang/cpython-cache}"
PYTHON_INSTALL="$HOME/.fang/python312-test"

# ── Locate a Python 3.12 interpreter ─────────────────────────────────────────

if [ ! -f "$PYTHON_INSTALL/python/bin/python3.12" ]; then
    TARBALL="$CACHE_DIR/cpython-${PYTHON_VERSION}-$(uname -m)-apple-darwin-install_only.tar.gz"
    if [ ! -f "$TARBALL" ]; then
        echo "error: install_only tarball not found: $TARBALL" >&2
        echo "Run a build first to populate the cpython cache." >&2
        exit 1
    fi
    mkdir -p "$PYTHON_INSTALL"
    tar -xzf "$TARBALL" -C "$PYTHON_INSTALL" 2>/dev/null
fi

PYTHON312="$PYTHON_INSTALL/python/bin/python3.12"
PYTHONHOME="$PYTHON_INSTALL/python"
echo "Using: $($PYTHONHOME/bin/python3.12 --version 2>&1)"

# ── Compile source files ──────────────────────────────────────────────────────

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

PYTHONHOME="$PYTHONHOME" "$PYTHON312" -c "
import py_compile, sys
for src, dst in zip(sys.argv[1::2], sys.argv[2::2]):
    py_compile.compile(src, cfile=dst, doraise=True)
" \
    "$SCRIPT_DIR/hello/__init__.py" "$WORKDIR/__init__.pyc" \
    "$SCRIPT_DIR/hello/__main__.py" "$WORKDIR/__main__.pyc"

# ── Build fang archive ────────────────────────────────────────────────────────

(cd "$WORKSPACE" && \
    FANG_PYTHON_VERSION="$PYTHON_VERSION" \
    "${CARGO:-cargo}" run --quiet -p fang-runtime --bin build-hello-archive -- \
        "$WORKDIR/__init__.pyc" \
        "$WORKDIR/__main__.pyc" \
        "$WORKDIR/hello.fang" \
        "$PYTHON_VERSION") 2>&1

echo "archive: $(wc -c < "$WORKDIR/hello.fang") bytes"

# ── Build fang-runtime with archive section embedded ─────────────────────────

(cd "$WORKSPACE" && \
    FANG_PYTHON_VERSION="$PYTHON_VERSION" \
    FANG_ARCHIVE="$WORKDIR/hello.fang" \
    "${CARGO:-cargo}" build --quiet -p fang-runtime --bin fang-runtime) 2>&1

# ── Prepare executable with archive embedded ──────────────────────────────────

RUNNER="$WORKSPACE/target/debug/fang-runtime"
if [ "$(uname -s)" = "Darwin" ]; then
    # macOS runtime loading uses the same append + trailer format as fang build.
    RUNNER="$WORKDIR/fang-runtime"
    cp "$WORKSPACE/target/debug/fang-runtime" "$RUNNER"
    chmod +x "$RUNNER"
    cat "$WORKDIR/hello.fang" >> "$RUNNER"
    ARCHIVE_LEN="$(wc -c < "$WORKDIR/hello.fang" | tr -d '[:space:]')"
    "$PYTHON312" -c 'import sys, struct; sys.stdout.buffer.write(struct.pack("<Q", int(sys.argv[1])) + b"FANGPACK")' "$ARCHIVE_LEN" >> "$RUNNER"
fi

# ── Run and verify ────────────────────────────────────────────────────────────

EXPECTED="hello%20from%20fang%21"
set +e
ACTUAL=$(PYTHONHOME="$PYTHONHOME" "$RUNNER" 2>&1)
EXIT=$?
set -e

if [ $EXIT -eq 0 ] && [ "$ACTUAL" = "$EXPECTED" ]; then
    echo "PASS: '$ACTUAL'"
    exit 0
else
    echo "FAIL (exit=$EXIT): expected '$EXPECTED', got '$ACTUAL'" >&2
    exit 1
fi
