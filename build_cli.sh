#!/usr/bin/env bash
# Build the fang CLI from pre-built runtimes.
#
# Usage:
#   ./build_cli.sh                           # builds macOS arm64 + x86_64 from runtime-dist/
#   FANG_RUNTIME_PATH=./path ./build_cli.sh  # builds exactly one CLI (used by CI)
#
# Output: dist/fang-{platform}  (e.g. dist/fang-macos-arm64, dist/fang-linux-x86_64)

set -euo pipefail
export LC_ALL=C
export LANG=C

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "${REPO_ROOT}/dist"
cd "$REPO_ROOT"

sha256_of() {
    if command -v sha256sum &>/dev/null; then sha256sum "$1" | awk '{print $1}'
    else shasum -a 256 "$1" | awk '{print $1}'
    fi
}

triple_to_platform() {
    case "$1" in
        aarch64-apple-darwin)      echo "macos-arm64" ;;
        x86_64-apple-darwin)       echo "macos-x86_64" ;;
        aarch64-unknown-linux-gnu) echo "linux-arm64" ;;
        x86_64-unknown-linux-gnu)  echo "linux-x86_64" ;;
        *) echo "ERROR: Unknown triple: $1" >&2; exit 1 ;;
    esac
}

# Extract the Rust triple from a runtime filename: fang-runtime-3.13-aarch64-apple-darwin
triple_from_runtime_path() {
    local name; name="$(basename "$1")"
    echo "$name" | sed -E 's/^fang-runtime-[0-9]+(\.[0-9]+){1,2}(\+[0-9]+)?-//'
}

build_one() {
    local runtime="$1"
    local triple platform out
    triple="$(triple_from_runtime_path "$runtime")"
    platform="$(triple_to_platform "$triple")"
    out="${REPO_ROOT}/dist/fang-${platform}"

    echo "==> Building CLI for ${platform}"
    echo "    Runtime: $(basename "$runtime")"
    echo "    Output:  ${out}"
    FANG_RUNTIME_PATH="$runtime" uv run python -m fang build --target "$platform" "$out"

    if [[ "$(uname -s)" == "Darwin" ]]; then
        echo "==> Signing ${out}"
        codesign --force -s - "$out"
    fi

    sha256_of "$out" > "${out}.sha256"
    echo "==> Wrote ${out}"
}

latest_runtime_for_triple() {
    local triple="$1"
    local candidates=()
    local runtime
    for runtime in "runtime-dist"/fang-runtime-*-"${triple}"; do
        [[ -f "$runtime" && "$runtime" != *.sha256 ]] || continue
        candidates+=("$runtime")
    done
    if [[ ${#candidates[@]} -eq 0 ]]; then
        return 1
    fi
    printf "%s\n" "${candidates[@]}" | sort -r | head -n 1
}

if [[ -n "${FANG_RUNTIME_PATH:-}" ]]; then
    build_one "$FANG_RUNTIME_PATH"
else
    [[ "$(uname -s)" == "Darwin" ]] || {
        echo "ERROR: Local CLI builds scan macOS runtimes only." >&2
        echo "       For Linux CI, set FANG_RUNTIME_PATH explicitly." >&2
        exit 1
    }

    runtimes=()
    for triple in aarch64-apple-darwin x86_64-apple-darwin; do
        runtime="$(latest_runtime_for_triple "$triple")" || {
            echo "ERROR: Missing runtime-dist/fang-runtime-*-${triple}" >&2
            echo "       Run: ./build_runtime.sh" >&2
            exit 1
        }
        runtimes+=("$runtime")
    done

    for runtime in "${runtimes[@]}"; do
        build_one "$runtime"
    done
fi

echo ""
ls -lh dist/fang-*
