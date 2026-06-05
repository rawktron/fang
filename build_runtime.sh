#!/usr/bin/env bash
# Build fang-runtime. No env vars needed.
#
# Usage:
#   ./build_runtime.sh                              # host platform (both arches), Python 3.13
#   ./build_runtime.sh 3.12                        # Python 3.12, host platform
#   ./build_runtime.sh 3.13 aarch64-apple-darwin   # one specific target (used by CI)
#
# Output: runtime-dist/fang-runtime-{python}-{target}
#
# Optional:
#   FANG_CPYTHON_TARBALL=/path/to/cpython-...tar.zst  # use an already-known tarball
#   FANG_CPYTHON_REPROBE=1                            # ignore cached .compat/.incompat sidecars

set -euo pipefail
export LC_ALL=C
export LANG=C

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT="${REPO_ROOT}/runtime-dist"
DEFAULT_PY="3.13"
PBS_RELEASES_API="https://api.github.com/repos/astral-sh/python-build-standalone/releases"
PBS_RELEASE_DOWNLOAD="https://github.com/astral-sh/python-build-standalone/releases/download"
RELEASES_PER_PAGE=10
CPYTHON_VARIANT_SUFFIX="pgo+lto-full.tar.zst"
COMPAT_MARKER="fang-runtime-libpython-probe-v2"

mkdir -p "$OUT"
cd "$REPO_ROOT"

PY="${1:-$DEFAULT_PY}"
SPECIFIC_TARGET="${2:-}"

# PYO3_CONFIG_FILE is always derived from the Python version; never set it manually.
pyo3_cfg() { echo "${REPO_ROOT}/.cargo/pyo3-cpython-${1}.cfg"; }

log() {
    printf "==> %s\n" "$*" >&2
}

warn() {
    printf "warning: %s\n" "$*" >&2
}

cache_dir() {
    echo "${FANG_CPYTHON_CACHE:-${HOME}/.fang/cpython-cache}"
}

sha256_of() {
    if command -v sha256sum &>/dev/null; then sha256sum "$1" | awk '{print $1}'
    else shasum -a 256 "$1" | awk '{print $1}'
    fi
}

download_to() {
    local url="$1" out="$2"
    curl --fail --location --silent --show-error --retry 2 --retry-delay 1 \
        -H "User-Agent: fang-runtime-build" \
        -o "$out" "$url"
}

platform_segment() {
    case "$1" in
        x86_64-unknown-linux-gnu)  echo "x86_64-unknown-linux-gnu" ;;
        aarch64-unknown-linux-gnu) echo "aarch64-unknown-linux-gnu" ;;
        x86_64-apple-darwin)       echo "x86_64-apple-darwin" ;;
        aarch64-apple-darwin)      echo "aarch64-apple-darwin" ;;
        *) echo "ERROR: Unsupported target: $1" >&2; exit 1 ;;
    esac
}

target_arch_for_ld() {
    case "$1" in
        aarch64-apple-darwin) echo "arm64" ;;
        x86_64-apple-darwin)  echo "x86_64" ;;
        *) echo "" ;;
    esac
}

version_kind() {
    local version="$1"
    if [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+\+[0-9]+$ ]]; then
        echo "concrete"
    elif [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "patch"
    elif [[ "$version" =~ ^[0-9]+\.[0-9]+$ ]]; then
        echo "series"
    else
        echo "ERROR: invalid Python version request: ${version}" >&2
        exit 1
    fi
}

series_from_version() {
    local base="${1%%+*}"
    local major minor rest
    IFS=. read -r major minor rest <<<"$base"
    echo "${major}.${minor}"
}

artifact_name_for() {
    local concrete="$1" platform="$2"
    local base="${concrete%%+*}"
    local tag="${concrete#*+}"
    echo "cpython-${base}+${tag}-${platform}-${CPYTHON_VARIANT_SUFFIX}"
}

release_tag_for() {
    echo "${1#*+}"
}

release_index_path() {
    echo "$(cache_dir)/release_index.json"
}

fetch_release_page() {
    local page="$1" out="$2"
    download_to "${PBS_RELEASES_API}?per_page=${RELEASES_PER_PAGE}&page=${page}" "$out"
}

write_index_from_pages() {
    local index_path="$1"
    shift
    python3 - "$index_path" "$@" <<'PY'
import json
import sys
from pathlib import Path

index_path = Path(sys.argv[1])
seen = set()
releases = []
for page_path in sys.argv[2:]:
    for release in json.loads(Path(page_path).read_text()):
        tag = release["tag_name"]
        if tag in seen:
            continue
        seen.add(tag)
        releases.append({
            "tag_name": tag,
            "assets": [asset["name"] for asset in release.get("assets", [])],
        })
index_path.parent.mkdir(parents=True, exist_ok=True)
index_path.write_text(json.dumps({"fetched_all": True, "releases": releases}, separators=(",", ":")))
PY
}

merge_page1_into_index() {
    local index_path="$1" page_path="$2"
    python3 - "$index_path" "$page_path" <<'PY'
import json
import sys
from pathlib import Path

index_path = Path(sys.argv[1])
page_path = Path(sys.argv[2])
try:
    index = json.loads(index_path.read_text())
except Exception:
    index = {"fetched_all": False, "releases": []}

known = {release.get("tag_name") for release in index.get("releases", [])}
new = []
for release in json.loads(page_path.read_text()):
    tag = release["tag_name"]
    if tag not in known:
        new.append({
            "tag_name": tag,
            "assets": [asset["name"] for asset in release.get("assets", [])],
        })
index["releases"] = new + index.get("releases", [])
index["fetched_all"] = bool(index.get("fetched_all"))
index_path.parent.mkdir(parents=True, exist_ok=True)
index_path.write_text(json.dumps(index, separators=(",", ":")))
PY
}

index_fetched_all() {
    local index_path="$1"
    [[ -f "$index_path" ]] || return 1
    python3 - "$index_path" <<'PY'
import json
import sys
try:
    raise SystemExit(0 if json.loads(open(sys.argv[1]).read()).get("fetched_all") else 1)
except Exception:
    raise SystemExit(1)
PY
}

ensure_release_index() {
    local dir index_path tmpdir page page_file count
    dir="$(cache_dir)"
    index_path="$(release_index_path)"
    mkdir -p "$dir"
    tmpdir="$(mktemp -d)"

    if index_fetched_all "$index_path"; then
        log "Checking python-build-standalone release index for new releases"
        page_file="${tmpdir}/page1.json"
        if fetch_release_page 1 "$page_file"; then
            merge_page1_into_index "$index_path" "$page_file"
        elif [[ ! -f "$index_path" ]]; then
            echo "ERROR: Could not fetch python-build-standalone release index and no cache exists." >&2
            rm -rf "$tmpdir"
            exit 1
        else
            warn "Could not refresh release index; using cached ${index_path}"
        fi
        rm -rf "$tmpdir"
        return
    fi

    log "Fetching full python-build-standalone release index"
    page=1
    local pages=()
    while true; do
        log "Fetching release index page ${page}"
        page_file="${tmpdir}/page${page}.json"
        if ! fetch_release_page "$page" "$page_file"; then
            if [[ -f "$index_path" ]]; then
                warn "Could not fetch release index page ${page}; using cached ${index_path}"
                rm -rf "$tmpdir"
                return
            fi
            echo "ERROR: Could not fetch python-build-standalone release index and no cache exists." >&2
            rm -rf "$tmpdir"
            exit 1
        fi
        pages+=("$page_file")
        count="$(python3 - "$page_file" <<'PY'
import json
import sys
print(len(json.loads(open(sys.argv[1]).read())))
PY
)"
        [[ "$count" -lt "$RELEASES_PER_PAGE" ]] && break
        page=$((page + 1))
    done

    write_index_from_pages "$index_path" "${pages[@]}"
    rm -rf "$tmpdir"
}

candidate_versions() {
    local requested="$1" platform="$2" variant="$3" index_path="$4"
    python3 - "$requested" "$platform" "$variant" "$index_path" <<'PY'
import json
import re
import sys

requested, platform, variant, index_path = sys.argv[1:5]
index = json.loads(open(index_path).read())

if re.fullmatch(r"\d+\.\d+\.\d+\+\d+", requested):
    print(requested)
    raise SystemExit(0)
if re.fullmatch(r"\d+\.\d+\.\d+", requested):
    kind = "patch"
elif re.fullmatch(r"\d+\.\d+", requested):
    kind = "series"
else:
    raise SystemExit(f"invalid Python version request: {requested}")

def parse_artifact(name):
    if not name.startswith("cpython-"):
        return None
    rest = name[len("cpython-"):]
    if "+" not in rest:
        return None
    pyver, rest = rest.split("+", 1)
    suffixes = ["install_only.tar.gz", "pgo+lto-full.tar.zst"]
    platforms = [
        "x86_64-unknown-linux-gnu",
        "aarch64-unknown-linux-gnu",
        "x86_64-apple-darwin",
        "aarch64-apple-darwin",
    ]
    for suffix in suffixes:
        tail = "-" + suffix
        if not rest.endswith(tail):
            continue
        before_variant = rest[:-len(tail)]
        for plat in platforms:
            plat_tail = "-" + plat
            if before_variant.endswith(plat_tail):
                tag = before_variant[:-len(plat_tail)]
                if tag:
                    return pyver, tag, plat, suffix
    return None

for release in index.get("releases", []):
    best = None
    for asset in release.get("assets", []):
        parsed = parse_artifact(asset)
        if not parsed:
            continue
        pyver, tag, plat, suffix = parsed
        if plat != platform or suffix != variant:
            continue
        if kind == "series":
            if pyver != requested and not pyver.startswith(requested + "."):
                continue
            try:
                patch = int(pyver.split(".")[2])
            except Exception:
                continue
        else:
            if pyver != requested:
                continue
            patch = 0
        concrete = f"{pyver}+{tag}"
        if best is None or patch > best[0]:
            best = (patch, concrete)
        if kind == "patch":
            break
    if best is not None:
        print(best[1])
PY
}

expected_hash_for() {
    local artifact="$1" tag="$2"
    local sums hash
    sums="$(mktemp)"
    if ! download_to "${PBS_RELEASE_DOWNLOAD}/${tag}/SHA256SUMS" "$sums"; then
        rm -f "$sums"
        return 1
    fi
    if hash="$(awk -v artifact="$artifact" '
        {
            name = $2
            sub(/^\*/, "", name)
            sub(/^\.\//, "", name)
            if (name == artifact) {
                print $1
                found = 1
                exit
            }
        }
        END { if (!found) exit 1 }
    ' "$sums")"; then
        rm -f "$sums"
        printf "%s\n" "$hash"
        return 0
    fi
    rm -f "$sums"
    return 1
}

fetch_and_cache() {
    local concrete="$1" platform="$2"
    local dir artifact tag tarball sidecar expected actual tmp
    dir="$(cache_dir)"
    artifact="$(artifact_name_for "$concrete" "$platform")"
    tag="$(release_tag_for "$concrete")"
    tarball="${dir}/${artifact}"
    sidecar="${dir}/${artifact}.sha256"
    mkdir -p "$dir"

    if [[ -f "$tarball" && -f "$sidecar" ]]; then
        log "Verifying cached CPython tarball: ${artifact}"
        expected="$(tr -d '[:space:]' < "$sidecar")"
        actual="$(sha256_of "$tarball")"
        if [[ "$actual" == "$expected" ]]; then
            log "Using cached CPython tarball: ${tarball}"
            echo "$tarball"
            return 0
        fi
        warn "Cached CPython tarball failed SHA256 verification; re-downloading ${artifact}"
        rm -f "$tarball" "$sidecar"
    fi

    log "Fetching SHA256SUMS for CPython release ${tag}"
    expected="$(expected_hash_for "$artifact" "$tag")" || {
        warn "${artifact} not found in SHA256SUMS for release ${tag}; trying the next candidate"
        return 1
    }
    tmp="${tarball}.tmp"
    log "Downloading CPython tarball: ${artifact}"
    download_to "${PBS_RELEASE_DOWNLOAD}/${tag}/${artifact}" "$tmp" || {
        rm -f "$tmp"
        warn "Could not download ${artifact}; trying the next candidate"
        return 1
    }
    log "Verifying SHA256 for ${artifact}"
    actual="$(sha256_of "$tmp")"
    if [[ "$actual" != "$expected" ]]; then
        rm -f "$tmp"
        echo "ERROR: SHA256 mismatch for ${artifact}: expected ${expected}, got ${actual}" >&2
        return 1
    fi
    mv "$tmp" "$tarball"
    printf "%s\n" "$expected" > "$sidecar"
    log "Cached CPython tarball: ${tarball}"
    echo "$tarball"
}

compat_probe_enabled() {
    local target="$1"
    if [[ "$(uname -s)" == "Darwin" && "$target" == *apple-darwin ]]; then
        return 0
    fi
    if [[ "$(uname -s)" == "Linux" && "$target" == *linux* ]] &&
        command -v clang >/dev/null && rust_lld_path "$target" >/dev/null; then
        return 0
    fi
    return 1
}

rust_lld_dir() {
    local target="$1" sysroot dir
    sysroot="$(rustc --print sysroot 2>/dev/null || true)"
    [[ -n "$sysroot" ]] || return 1
    dir="${sysroot}/lib/rustlib/${target}/bin/gcc-ld"
    [[ -d "$dir" ]] || return 1
    echo "$dir"
}

rust_lld_path() {
    local target="$1" dir
    dir="$(rust_lld_dir "$target")" || return 1
    if [[ -x "${dir}/ld.lld" ]]; then
        echo "${dir}/ld.lld"
        return 0
    fi
    if [[ -x "${dir}/rust-lld" ]]; then
        echo "${dir}/rust-lld"
        return 0
    fi
    return 1
}

compat_linker_version() {
    local target="$1"
    if [[ "$(uname -s)" == "Darwin" && "$target" == *apple-darwin ]]; then
        ld -v 2>&1 | sed -n '1p' | tr -d '\r'
        return 0
    fi
    if [[ "$(uname -s)" == "Linux" && "$target" == *linux* ]]; then
        local lld
        lld="$(rust_lld_path "$target")"
        {
            clang --version 2>&1 | sed -n '1p'
            "$lld" --version 2>&1 | sed -n '1p'
        } | tr -d '\r'
        return 0
    fi
}

compat_version_path() {
    local target="$1" safe_target
    safe_target="$(printf "%s" "$target" | tr -c '[:alnum:]_.-' '_')"
    echo "$(cache_dir)/linker_version_${safe_target}.txt"
}

invalidate_compat_sidecars_if_needed() {
    local target="$1"
    local dir version_path current stored
    compat_probe_enabled "$target" || return 0
    dir="$(cache_dir)"
    mkdir -p "$dir"
    version_path="$(compat_version_path "$target")"
    current="$(compat_linker_version "$target")"
    stored=""
    [[ -f "$version_path" ]] && stored="$(tr -d '\r' < "$version_path")"
    if [[ "$stored" == "$current" ]]; then
        return 0
    fi
    find "$dir" -maxdepth 1 \( -name '*.compat' -o -name '*.incompat' \) -type f -print |
        while IFS= read -r sidecar; do
            rm -f "$sidecar"
        done
    printf "%s\n" "$current" > "$version_path"
    log "Linker changed; invalidated CPython compatibility sidecars"
}

probe_static_lib_compat() {
    local tarball="$1" concrete="$2" target="$3" platform="$4"
    local dir artifact arch series libfile member tmpdir out
    compat_probe_enabled "$target" || return 0

    dir="$(cache_dir)"
    artifact="$(artifact_name_for "$concrete" "$platform")"
    if [[ -z "${FANG_CPYTHON_REPROBE:-}" && -f "${dir}/${artifact}.incompat" ]]; then
        if [[ "$(cat "${dir}/${artifact}.incompat" 2>/dev/null || true)" == "$COMPAT_MARKER" ]]; then
            log "Skipping known-incompatible CPython artifact: ${artifact}"
            return 1
        fi
        rm -f "${dir}/${artifact}.incompat"
    fi
    if [[ -z "${FANG_CPYTHON_REPROBE:-}" && -f "${dir}/${artifact}.compat" ]]; then
        if [[ "$(cat "${dir}/${artifact}.compat" 2>/dev/null || true)" == "$COMPAT_MARKER" ]]; then
            log "Using cached linker compatibility result: ${artifact}"
            return 0
        fi
        rm -f "${dir}/${artifact}.compat"
    fi

    arch="$(target_arch_for_ld "$target")"
    series="$(series_from_version "$concrete")"
    libfile="libpython${series}.a"
    member="$(tar --zstd -tf "$tarball" 2>/dev/null | awk -v lib="$libfile" '
        $0 ~ "/" lib "$" {
            if ($0 ~ "/config-") {
                print
                found = 1
                exit
            }
            if (!fallback) {
                fallback = $0
            }
        }
        END {
            if (!found && fallback) {
                print fallback
            }
        }
    ')"
    if [[ -z "$member" ]]; then
        warn "Could not find ${libfile} for linker compatibility probe; accepting ${artifact}"
        printf "%s\n" "$COMPAT_MARKER" > "${dir}/${artifact}.compat"
        return 0
    fi

    log "Probing linker compatibility for ${artifact}"
    tmpdir="$(mktemp -d)"
    if ! tar -x --zstd -f "$tarball" -C "$tmpdir" "$member" 2>/dev/null; then
        rm -rf "$tmpdir"
        warn "Could not extract ${libfile} for linker compatibility probe; trying the next candidate"
        printf "%s\n" "$COMPAT_MARKER" > "${dir}/${artifact}.incompat"
        rm -f "$tarball" "${tarball}.sha256" "${dir}/${artifact}.compat"
        return 1
    fi

    if [[ "$(uname -s)" == "Darwin" ]]; then
        out="$(ld -arch "$arch" -o /dev/null "${tmpdir}/${member}" 2>&1 || true)"
    else
        local lld
        lld="$(rust_lld_path "$target")"
        out="$("$lld" -r -o /dev/null --whole-archive "${tmpdir}/${member}" --no-whole-archive 2>&1 || true)"
    fi
    rm -rf "$tmpdir"

    if grep -q "Unknown attribute kind\|could not parse bitcode\|Invalid record\|file format not recognized" <<<"$out"; then
        warn "CPython artifact is incompatible with host linker; trying the next candidate: ${artifact}"
        printf "%s\n" "$COMPAT_MARKER" > "${dir}/${artifact}.incompat"
        rm -f "$tarball" "${tarball}.sha256" "${dir}/${artifact}.compat"
        return 1
    fi

    printf "%s\n" "$COMPAT_MARKER" > "${dir}/${artifact}.compat"
    return 0
}

known_incompatible() {
    local concrete="$1" target="$2" platform="$3"
    local dir artifact
    compat_probe_enabled "$target" || return 1
    [[ -n "${FANG_CPYTHON_REPROBE:-}" ]] && return 1
    dir="$(cache_dir)"
    artifact="$(artifact_name_for "$concrete" "$platform")"
    [[ -f "${dir}/${artifact}.incompat" ]] &&
        [[ "$(cat "${dir}/${artifact}.incompat" 2>/dev/null || true)" == "$COMPAT_MARKER" ]]
}

ensure_cpython_tarball() {
    local py="$1" target="$2"
    local platform index kind candidates concrete tarball

    if [[ -n "${FANG_CPYTHON_TARBALL:-}" ]]; then
        if [[ ! -f "$FANG_CPYTHON_TARBALL" ]]; then
            echo "ERROR: FANG_CPYTHON_TARBALL does not point to a file: ${FANG_CPYTHON_TARBALL}" >&2
            exit 1
        fi
        log "Using FANG_CPYTHON_TARBALL override: ${FANG_CPYTHON_TARBALL}"
        echo "$FANG_CPYTHON_TARBALL"
        return 0
    fi

    platform="$(platform_segment "$target")"
    kind="$(version_kind "$py")"
    log "Resolving CPython ${py} for ${platform}"
    log "CPython cache: $(cache_dir)"
    invalidate_compat_sidecars_if_needed "$target"

    if [[ "$kind" == "concrete" ]]; then
        candidates=("$py")
    else
        ensure_release_index
        index="$(release_index_path)"
        candidates=()
        while IFS= read -r concrete; do
            [[ -n "$concrete" ]] && candidates+=("$concrete")
        done < <(candidate_versions "$py" "$platform" "$CPYTHON_VARIANT_SUFFIX" "$index")
    fi
    if [[ ${#candidates[@]} -eq 0 ]]; then
        echo "ERROR: Could not resolve CPython ${py} for ${platform} (${CPYTHON_VARIANT_SUFFIX})." >&2
        exit 1
    fi
    log "Found ${#candidates[@]} CPython candidate(s)"

    local skipped_incompatible=0
    local total_skipped_incompatible=0
    local probed=0
    local unavailable=0
    for concrete in "${candidates[@]}"; do
        if known_incompatible "$concrete" "$target" "$platform"; then
            skipped_incompatible=$((skipped_incompatible + 1))
            total_skipped_incompatible=$((total_skipped_incompatible + 1))
            continue
        fi
        if [[ "$skipped_incompatible" -gt 0 ]]; then
            log "Skipped ${skipped_incompatible} candidate(s) already marked incompatible with this linker"
            skipped_incompatible=0
        fi
        log "Trying CPython candidate ${concrete}"
        tarball="$(fetch_and_cache "$concrete" "$platform")" || {
            unavailable=$((unavailable + 1))
            continue
        }
        probed=$((probed + 1))
        if probe_static_lib_compat "$tarball" "$concrete" "$target" "$platform"; then
            echo "$tarball"
            return 0
        fi
    done
    if [[ "$skipped_incompatible" -gt 0 ]]; then
        log "Skipped ${skipped_incompatible} candidate(s) already marked incompatible with this linker"
    fi

    if [[ "$(uname -s)" == "Darwin" && "$target" == *apple-darwin ]]; then
        echo "ERROR: No compatible CPython ${py} artifact found for ${platform}." >&2
        echo "       Checked ${probed} candidate(s), skipped ${total_skipped_incompatible} cached incompatible candidate(s), ${unavailable} unavailable candidate(s)." >&2
        echo "       Incompatible candidates are cached as .incompat sidecars and are invalidated when ld -v changes." >&2
        echo "       To retry probes after changing the script or cache state: FANG_CPYTHON_REPROBE=1 ./build_runtime.sh ${py} ${target}" >&2
        echo "       To use a known-good archive directly: FANG_CPYTHON_TARBALL=/path/to/cpython-...tar.zst ./build_runtime.sh ${py} ${target}" >&2
    else
        echo "ERROR: No compatible CPython ${py} artifact found for ${platform}." >&2
    fi
    exit 1
}

# Returns the two Rust target triples for the current host OS.
host_targets() {
    case "$(uname -s)" in
        Darwin) echo "aarch64-apple-darwin"; echo "x86_64-apple-darwin" ;;
        Linux)  echo "aarch64-unknown-linux-gnu"; echo "x86_64-unknown-linux-gnu" ;;
        *) echo "ERROR: Unsupported OS: $(uname -s)" >&2; exit 1 ;;
    esac
}

build_one() {
    local py="$1" target="$2"
    local tarball asset
    if [[ "$target" == *linux* && "$(uname -s)" != "Linux" ]]; then
        echo "ERROR: Linux runtime targets are built in CI, not locally on macOS." >&2
        echo "       Push a fang-runtime-v* tag to let GitHub Actions build ${target}." >&2
        exit 1
    fi

    log "Preparing runtime build for Python ${py} target ${target}"
    rustup target add "$target" 2>/dev/null || true
    tarball="$(ensure_cpython_tarball "$py" "$target")"
    asset="fang-runtime-${py}-${target}"

    log "Building ${asset} with cargo"

    local cargo_env=(
        "FANG_PYTHON_VERSION=$py"
        "FANG_CPYTHON_TARBALL=$tarball"
        "PYO3_CONFIG_FILE=$(pyo3_cfg "$py")"
    )
    if [[ "$target" == *linux* ]] && command -v clang >/dev/null && rust_lld_dir "$target" >/dev/null; then
        local linker_var target_env rustflags lld_dir
        target_env="$(printf "%s" "$target" | tr '[:lower:]-' '[:upper:]_')"
        linker_var="CARGO_TARGET_${target_env}_LINKER"
        rustflags="${RUSTFLAGS:-}"
        lld_dir="$(rust_lld_dir "$target")"
        log "Using clang with Rust bundled lld for ${target}"
        cargo_env+=("${linker_var}=clang")
        cargo_env+=("RUSTFLAGS=${rustflags:+${rustflags} }-C link-arg=-B${lld_dir} -C link-arg=-fuse-ld=lld")
    fi

    env "${cargo_env[@]}" cargo build -p fang-runtime --release --target "$target"

    cp "target/${target}/release/fang-runtime" "${OUT}/${asset}"
    sha256_of "${OUT}/${asset}" > "${OUT}/${asset}.sha256"
    log "Wrote ${OUT}/${asset}"
}

if [[ -n "$SPECIFIC_TARGET" ]]; then
    build_one "$PY" "$SPECIFIC_TARGET"
else
    while IFS= read -r target; do
        build_one "$PY" "$target"
    done < <(host_targets)
fi

echo ""
ls -lh "${OUT}/"
