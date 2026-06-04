#!/usr/bin/env bash
# Release pre-built assets only. This script never builds.
#
# Usage:
#   ./release.sh runtime fang-runtime-v0.1.0
#   ./release.sh cli v0.1.0

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_ROOT"

[[ $# -eq 2 ]] || { echo "Usage: ./release.sh (runtime|cli) TAG"; exit 1; }
KIND="$1"
TAG="$2"

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "ERROR: Uncommitted changes. Commit or stash first." >&2
    exit 1
fi

if git rev-parse "$TAG" &>/dev/null 2>&1; then
    echo "ERROR: Tag '${TAG}' already exists." >&2
    exit 1
fi

require_file() {
    local path="$1"
    [[ -f "$path" ]] || {
        echo "ERROR: Missing required asset: $path" >&2
        return 1
    }
}

python_series() {
    python3 - <<'PY'
import re
from pathlib import Path

text = Path("fang.toml").read_text()
match = re.search(r'^\s*python\s*=\s*"([0-9]+)\.([0-9]+)', text, re.M)
if not match:
    raise SystemExit("ERROR: Could not find [project] python in fang.toml")
print(f"{match.group(1)}.{match.group(2)}")
PY
}

release_runtime() {
    local tag="$1"
    [[ "$tag" == fang-runtime-v* ]] || { echo "ERROR: Tag must start with 'fang-runtime-v'"; exit 1; }

    local py
    py="$(python_series)"
    local assets=(
        "runtime-dist/fang-runtime-${py}-aarch64-apple-darwin"
        "runtime-dist/fang-runtime-${py}-aarch64-apple-darwin.sha256"
        "runtime-dist/fang-runtime-${py}-x86_64-apple-darwin"
        "runtime-dist/fang-runtime-${py}-x86_64-apple-darwin.sha256"
    )
    for f in "${assets[@]}"; do
        require_file "$f" || {
            echo "       Run: ./build_runtime.sh ${py}" >&2
            exit 1
        }
    done

    echo "Uploading ${#assets[@]} macOS runtime assets for Python ${py} to ${tag}..."
    git tag "$tag"
    gh release create "$tag" \
        --title "$tag" \
        --notes "Runtime binaries for ${tag}." \
        --prerelease \
        "${assets[@]}"

    git push origin "$tag"
    echo "Done. CI is building Linux runtime assets."
}

release_cli() {
    local tag="$1"
    [[ "$tag" == v* ]] || { echo "ERROR: Tag must start with 'v'"; exit 1; }

    local assets=(
        dist/fang-macos-arm64
        dist/fang-macos-arm64.sha256
        dist/fang-macos-x86_64
        dist/fang-macos-x86_64.sha256
    )
    for f in "${assets[@]}"; do
        require_file "$f" || {
            echo "       Run: ./build_cli.sh" >&2
            exit 1
        }
    done

    echo "Uploading ${#assets[@]} macOS CLI assets to ${tag}..."
    git tag "$tag"
    gh release create "$tag" \
        --title "$tag" \
        --notes "fang CLI ${tag}." \
        --prerelease \
        "${assets[@]}"

    git push origin "$tag"
    echo "Done. CI is building Linux CLI assets."
}

case "$KIND" in
    runtime) release_runtime "$TAG" ;;
    cli)     release_cli "$TAG" ;;
    *) echo "ERROR: Use 'runtime' or 'cli'."; exit 1 ;;
esac
