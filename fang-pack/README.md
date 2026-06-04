# fang-pack

The fang asset archive format — a pure Rust library for building and reading the content-addressed, zstd-compressed archive that is embedded into every fang binary.

## Format

An archive is a single contiguous blob:

```
[ 64-byte header (magic + version + index offset + index length) ]
[ zstd-compressed blob store                                      ]
[ zstd-compressed index (JSON array of IndexEntry)               ]
```

Each `IndexEntry` records the asset path, its offset and length in the blob store, and a BLAKE3 content hash. The index is at the end so the blob store can be written streaming without seeking.

## Categories

Paths must begin with one of six category prefixes:

| Prefix | Contents |
|---|---|
| `stdlib/` | CPython standard library `.pyc` files |
| `site-packages/` | Pure-Python dependency `.pyc` files |
| `extensions/` | C extension blobs (`.so`/`.dylib`) |
| `native-libs/` | Bundled native shared libraries (libSDL2, etc.) |
| `app/` | The user's application `.pyc` files |
| `meta/` | Manifest and metadata JSON |

Paths with traversal sequences (`../`, absolute paths) are rejected at both write and read time.

## API

```rust
// Building
let mut builder = ArchiveBuilder::new();
builder.add("app/myapp/__main__.pyc", bytecode)?;
builder.set_meta(Meta { python_version: "3.12.0".into(), ... });
let bytes = builder.build()?;

// Reading
let archive = Archive::from_bytes(&bytes)?;
let data = archive.get("app/myapp/__main__.pyc")?; // Option<Vec<u8>>
let data = archive.get_verified("app/myapp/__main__.pyc")?; // checks BLAKE3 hash
let meta = archive.meta()?;
```

## Security properties

- Path traversal rejected at both `add()` and `get()` — an attacker cannot craft an archive that escapes the category layout
- `get_verified()` checks the BLAKE3 hash before returning data — tampered archives are detected
- Implausibly large entry counts are capped before allocation to prevent OOM on corrupt input
- Magic byte check on `from_bytes` prevents silent misinterpretation of arbitrary data
