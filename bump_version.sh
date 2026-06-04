#!/usr/bin/env bash
# Bump the version across all source files.
#
# Usage: ./bump_version.sh 0.2.0

set -euo pipefail

[[ $# -eq 1 ]] || { echo "Usage: ./bump_version.sh NEW_VERSION"; exit 1; }
NEW="$1"

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_ROOT"

# Validate semver-ish
[[ "$NEW" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "ERROR: Version must be X.Y.Z"; exit 1; }

echo "Bumping to ${NEW}..."

# Python: fang/__init__.py
sed -i '' "s/^__version__ = \".*\"/__version__ = \"${NEW}\"/" fang/__init__.py

# Python: pyproject.toml
sed -i '' "s/^version = \".*\"/version = \"${NEW}\"/" pyproject.toml

# Rust: all crate Cargo.toml files
for toml in fang-pack/Cargo.toml fang-runtime/Cargo.toml fang-importer/Cargo.toml; do
    sed -i '' "s/^version = \".*\"/version = \"${NEW}\"/" "$toml"
done

echo "Done. Files updated:"
grep -n "version" fang/__init__.py pyproject.toml fang-*/Cargo.toml | grep "$NEW"
