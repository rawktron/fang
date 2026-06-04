#!/usr/bin/env bash
# Integration tests for fang-runtime --fang-tool compile-bytecode.
# Requires: FANG_PYTHON_VERSION env var (e.g. FANG_PYTHON_VERSION=3.12.3+20240415)
# The binary is built WITHOUT an embedded archive to verify tool mode works
# without one.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="$(cd "$SCRIPT_DIR/../../.." && pwd)"
PYTHON_VERSION="${FANG_PYTHON_VERSION:-3.12.3+20240415}"
PYTHON_MINOR="$(echo "$PYTHON_VERSION" | sed 's/+.*//' | cut -d. -f1-2)"

PASS=0
FAIL=0

pass() { echo "PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "FAIL: $1" >&2; FAIL=$((FAIL + 1)); }

# ── Build fang-runtime without any embedded archive ───────────────────────────
(cd "$WORKSPACE" && \
    FANG_PYTHON_VERSION="$PYTHON_VERSION" \
    "${CARGO:-cargo}" build --quiet -p fang-runtime --bin fang-runtime) 2>&1

RUNNER="$WORKSPACE/target/debug/fang-runtime"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# ── Test 1: Successful compilation produces adjacent .pyc files ───────────────
mkdir -p "$WORKDIR/pkg"
cat > "$WORKDIR/pkg/__init__.py" << 'EOF'
from .mod import hello
EOF
cat > "$WORKDIR/pkg/mod.py" << 'EOF'
def hello():
    return "hello from fang!"
EOF

"$RUNNER" --fang-tool compile-bytecode \
    --python-version "$PYTHON_MINOR" \
    --in-place "$WORKDIR/pkg"

if [ -f "$WORKDIR/pkg/__init__.pyc" ] && [ -f "$WORKDIR/pkg/mod.pyc" ]; then
    pass "compile-bytecode creates adjacent .pyc files"
else
    fail "compile-bytecode did not create .pyc files"
fi

# ── Test 2: Compile failure exits nonzero and reports the failing file ────────
mkdir -p "$WORKDIR/bad"
cat > "$WORKDIR/bad/syntax_error.py" << 'EOF'
def broken(
    # unclosed parenthesis — SyntaxError
EOF

set +e
"$RUNNER" --fang-tool compile-bytecode \
    --python-version "$PYTHON_MINOR" \
    --in-place "$WORKDIR/bad" 2>&1
EXIT=$?
set -e

if [ $EXIT -ne 0 ]; then
    pass "compile failure exits nonzero (exit=$EXIT)"
else
    fail "expected nonzero exit for syntax error, got exit=0"
fi

# ── Test 3: Tool mode succeeds without an embedded app archive ────────────────
# The binary was built with no FANG_ARCHIVE, so Archive::from_current_binary
# would fail in normal mode. Tool mode must bypass that and succeed.
mkdir -p "$WORKDIR/simple"
echo "x = 42" > "$WORKDIR/simple/x.py"

"$RUNNER" --fang-tool compile-bytecode \
    --in-place "$WORKDIR/simple"

if [ -f "$WORKDIR/simple/x.pyc" ]; then
    pass "tool mode works without embedded archive"
else
    fail "tool mode failed or did not produce x.pyc without embedded archive"
fi

# ── Test 4: Python version mismatch exits nonzero ────────────────────────────
mkdir -p "$WORKDIR/version_check"
echo "y = 1" > "$WORKDIR/version_check/y.py"

WRONG_VERSION="2.7"
set +e
"$RUNNER" --fang-tool compile-bytecode \
    --python-version "$WRONG_VERSION" \
    --in-place "$WORKDIR/version_check" 2>&1
EXIT=$?
set -e

if [ $EXIT -ne 0 ]; then
    pass "Python version mismatch exits nonzero (exit=$EXIT)"
else
    fail "expected nonzero exit for Python version mismatch, got exit=0"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
if [ $FAIL -ne 0 ]; then
    exit 1
fi
