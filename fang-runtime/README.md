# fang-runtime

The launcher binary that is embedded into every fang output executable. When you run a fang-built app, you are running the fang-runtime with your app's asset archive fused into its binary section.

## Responsibilities

1. **CPython initialization** — starts the embedded CPython interpreter via the stable C embedding API, with `sys.prefix` and `sys.exec_prefix` redirected into the in-binary asset store
2. **Importer registration** — installs `FangImporter` (from `fang-importer`) onto `sys.meta_path` so all imports resolve from the archive
3. **C extension loading** — handles platform-specific loading of `.so`/`.dylib` blobs without extracting to a user-visible location:
   - **Linux**: `memfd_create` → write blob → `dlopen(/proc/self/fd/{fd})` — never touches disk
   - **macOS**: content-addressed cache in `~/Library/Caches/<app>/<hash>/` — extracted once, loaded from cache thereafter
4. **Entry point dispatch** — reads `meta.json` from the archive, sets up `sys.argv` and environment, and calls the app's entry point

## How the archive is found

The runtime locates the embedded archive by scanning its own binary for the `FANG` magic header. On Linux the assets live in the `.fang_assets` ELF section; on macOS in the `__FANG/__assets` Mach-O section.

## Build note

`fang-runtime` links `libpython3.X.a` statically. Build it through `./build_runtime.sh`, which fetches and verifies the appropriate [python-build-standalone](https://github.com/indygreg/python-build-standalone) tarball, then passes it to Cargo as `FANG_CPYTHON_TARBALL`.

## Key source files

| File | Description |
|---|---|
| `src/main.rs` | Entry point — locates archive, initializes CPython, runs app |
| `src/python_init.rs` | CPython embedding and `sys.prefix` override |
| `src/entrypoint.rs` | Entry point module/callable dispatch |
| `src/frozen.rs` | Frozen bootstrap modules injected before the importer installs |
| `src/runtime_stdlib.rs` | Stdlib location from the bundled runtime artifact |
| `src/tool_mode.rs` | Runtime sub-commands used by `fang build` during the build process |
