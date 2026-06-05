```
  █████▒▄▄▄       ███▄    █   ▄████
▓██   ▒▒████▄     ██ ▀█   █  ██▒ ▀█▒
▒████ ░▒██  ▀█▄  ▓██  ▀█ ██▒▒██░▄▄▄░
░▓█▒  ░░██▄▄▄▄██ ▓██▒  ▐▌██▒░▓█  ██▓
░▒█░    ▓█   ▓██▒▒██░   ▓██░░▒▓███▀▒
 ▒ ░    ▒▒   ▓▒█░░ ▒░   ▒ ▒  ░▒   ▒
 ░       ▒   ▒▒ ░░ ░░   ░ ▒░  ░   ░
 ░ ░     ░   ▒      ░   ░ ░ ░ ░   ░
             ░  ░         ░       ░
```

`bun --compile` for Python. Ship your app as a single native binary — no runtime required on the target machine, no temp-directory extraction, no install step.

## Why

Python apps are hard to distribute. fang solves that by embedding a statically-linked CPython, your entire dependency tree (including C extensions), and all your code into one executable. The binary just runs.

On Linux, C extensions load via `memfd_create` and never touch disk. On macOS, they're cached in `~/Library/Caches/<app>/` on first run and loaded from there after. Either way, startup time is within ~50ms of a plain `python` invocation.

## Installation

```bash
brew tap rawktron/fang
brew install fang
```

Or install without tapping:

```bash
brew install rawktron/fang/fang
```

Manual release binaries are also available:

```bash
curl -L -o fang https://github.com/rawktron/fang/releases/download/v0.1.0/fang-macos-arm64
chmod +x fang
sudo mv fang /usr/local/bin/fang
```

## Usage

```bash
fang init          # discover project and write fang.toml
fang build         # produce a single binary in dist/
fang check         # preflight project and environment
fang inspect myapp # show embedded assets and metadata
```

## Configuration (`fang.toml`)

```toml
[project]
name = "myapp"
entry = "myapp.__main__"   # or "myapp:main"
python = "3.12.3"

[build]
strip = true
compress = "zstd"

[bundle]
native-libs = ["libSDL2"]  # usually auto-detected
```

## Supported app types

- Pure Python CLIs (`click`, `typer`, `argparse`)
- Rich/Textual TUI apps
- Apps with C extensions (`numpy`, `pandas`)
- pygame apps (SDL bundling included)

## Platforms

| Platform       | Status    |
|----------------|-----------|
| Linux x86_64   | Supported |
| Linux aarch64  | Supported |
| macOS arm64    | Supported |
| macOS x86_64   | Supported |
| Windows        | Not yet   |

## Development

The fang CLI is pure Python. You do **not** need a Rust toolchain to work on it.

```bash
git clone https://github.com/rawktron/fang
cd fang
uv sync
uv run fang --help
```

To run `fang build` you need a `fang-runtime` binary. Download one from the [latest release](https://github.com/rawktron/fang/releases/latest) and point `FANG_RUNTIME_PATH` at it:

```bash
export FANG_RUNTIME_PATH=/path/to/fang-runtime
uv run fang build myapp/
```

The Rust toolchain is only needed if you're modifying `fang-runtime` itself:

```bash
cargo build -p fang-runtime --release
export FANG_RUNTIME_PATH=./target/release/fang-runtime
```

## Workspace

| Component                          | Description                                                  |
|------------------------------------|--------------------------------------------------------------|
| [`fang/`](fang/)                   | The `fang` CLI (Python)                                      |
| [`fang-runtime/`](fang-runtime/)   | Launcher embedded in every output binary (Rust)              |
| [`fang-importer/`](fang-importer/) | Custom `sys.meta_path` importer (Rust, CPython C API)        |
| [`fang-pack/`](fang-pack/)         | Asset archive format — read/write, zstd, content-addressed   |

## Author

[Pete Garcin](mailto:pete@rawktron.com)

## License

Licensed under either of [Apache-2.0](LICENSE-APACHE) or [MIT](LICENSE-MIT) at your option.
