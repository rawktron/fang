# fang-importer

The `FangImporter` — a custom Python `sys.meta_path` finder and loader implemented in Rust using the CPython C API via `pyo3-ffi`. It is compiled into `fang-runtime` and is the mechanism by which all Python code inside a fang binary is imported at runtime.

## How it works

When CPython encounters an `import` statement, it walks `sys.meta_path` looking for a finder that knows about the module. `FangImporter` intercepts every import and checks the `fang-pack` archive embedded in the binary:

- **Pure Python modules** (`.pyc`): decompressed from the archive and loaded directly into CPython's memory — no filesystem access
- **C extensions** (`.so`/`.dylib`): handled by the platform-specific extension loader in `fang-runtime` (memfd on Linux, cache on macOS), then registered with CPython
- **Package data**: `__file__` is set to a synthetic `fang://` path; `__spec__.origin` is set accordingly

The importer also synthesizes `importlib.metadata` and `pkg_resources` distribution records so packages that inspect their own metadata at runtime don't break.

## Module structure

| File | Description |
|---|---|
| `src/lib.rs` | Crate root — exports the importer init symbol |
| `src/importer.rs` | `FangImporter` type: `find_module`, `load_module`, spec construction |
| `src/ext.rs` | C extension loading dispatch (delegates to platform loader) |
| `src/paths.rs` | Synthetic `fang://` path construction and `__file__` spoofing |

## Features

- `extension-module` — enables `pyo3-ffi`'s extension module ABI, used when building the importer as a standalone `.so` for testing

## Testing

Integration tests load the importer as a Python extension and exercise import of `.pyc` blobs from a real `fang-pack` archive. Run with:

```bash
cargo test --features extension-module
```
