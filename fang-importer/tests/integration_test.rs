/// Integration tests for fang-importer.
///
/// These tests build a small archive and run Python subprocesses that load
/// the fang_importer extension module and exercise import behaviour.
use std::path::PathBuf;
use std::process::Command;

fn python() -> &'static str {
    "/usr/local/bin/python3.11"
}

/// Path to the built extension .dylib, renamed to what Python expects.
fn extension_so() -> PathBuf {
    // Cargo puts the dylib here during debug builds.
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push("../target/debug/deps/libfang_importer.dylib");
    p
}

/// Build a small .pyc blob for the given Python source.
///
/// Returns the raw .pyc bytes (16-byte header + marshalled code object).
fn compile_pyc(source: &str) -> Vec<u8> {
    let dir = tempfile::tempdir().expect("tempdir");
    let src = dir.path().join("mod.py");
    std::fs::write(&src, source).unwrap();

    let out = Command::new(python())
        .args([
            "-c",
            &format!(
                "import py_compile, pathlib; \
                 py_compile.compile('{}', cfile='{}', doraise=True)",
                src.display(),
                dir.path().join("mod.pyc").display()
            ),
        ])
        .output()
        .expect("py_compile");
    assert!(out.status.success(), "py_compile failed: {:?}", out);
    std::fs::read(dir.path().join("mod.pyc")).unwrap()
}

/// Build a minimal fang archive containing one app/ module.
fn build_archive(module_name: &str, source: &str) -> Vec<u8> {
    let pyc = compile_pyc(source);
    // We invoke a Rust test helper binary — but since this is a pure Rust
    // integration test we can call the fang-pack library directly.
    use fang_pack::{ArchiveBuilder, Meta};

    let meta = Meta {
        python_version: "3.11".into(),
        entry_point: "app.main".into(),
        entry_callable: None,
        platform: "macos".into(),
        build_timestamp: "2024-01-01T00:00:00Z".into(),
        project_name: "testapp".into(),
        extensions: std::collections::HashMap::new(),
        native_libs: Vec::new(),
        rtld_global: true,
    };

    let path = format!("app/{}.pyc", module_name.replace('.', "/"));
    let mut builder = ArchiveBuilder::new();
    builder.set_meta(meta);
    builder.add(&path, &pyc).unwrap();
    builder.build().unwrap()
}

fn build_archive_with_entries(modules: &[(&str, &str)], files: &[(&str, &[u8])]) -> Vec<u8> {
    use fang_pack::{ArchiveBuilder, Meta};

    let meta = Meta {
        python_version: "3.11".into(),
        entry_point: "app.main".into(),
        entry_callable: None,
        platform: "macos".into(),
        build_timestamp: "2024-01-01T00:00:00Z".into(),
        project_name: "testapp".into(),
        extensions: std::collections::HashMap::new(),
        native_libs: Vec::new(),
        rtld_global: true,
    };

    let mut builder = ArchiveBuilder::new();
    builder.set_meta(meta);
    for (module_name, source) in modules {
        let pyc = compile_pyc(source);
        let path = format!("app/{}.pyc", module_name.replace('.', "/"));
        builder.add(&path, &pyc).unwrap();
    }
    for (path, data) in files {
        builder.add(path, data).unwrap();
    }
    builder.build().unwrap()
}

/// Run a Python snippet in a subprocess with the fang_importer .so on sys.path.
fn run_python_with_importer(archive_bytes: &[u8], snippet: &str) -> std::process::Output {
    // Symlink the dylib with the name Python expects: fang_importer.cpython-311-darwin.so
    // Actually, on macOS we can just use the .dylib name directly via ctypes.cdll
    // or by putting it on sys.path and renaming. The cleanest approach: copy to a
    // temp dir with the correct ABI tag name and add that dir to sys.path.
    let dir = tempfile::tempdir().expect("tempdir");
    let so_src = extension_so();
    assert!(so_src.exists(), "extension .so not found at {:?} — run `cargo build -p fang-importer --features extension-module` first", so_src);

    // Python looks for fang_importer.so or the ABI-tagged variant.
    let so_dst = dir.path().join("fang_importer.so");
    std::fs::copy(&so_src, &so_dst).unwrap();

    // Write the archive bytes to a file the Python script can read.
    let archive_path = dir.path().join("archive.fang");
    std::fs::write(&archive_path, archive_bytes).unwrap();

    let script = format!(
        r#"
import sys
sys.path.insert(0, r'{ext_dir}')
import fang_importer
archive_bytes = open(r'{archive}', 'rb').read()
fang_importer.install_from_bytes(archive_bytes)
{snippet}
"#,
        ext_dir = dir.path().display(),
        archive = archive_path.display(),
        snippet = snippet,
    );

    Command::new(python())
        .args(["-c", &script])
        .output()
        .expect("python subprocess")
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[test]
fn test_import_present_module() {
    let archive = build_archive("greet", "GREETING = 'hello from fang'");
    let out = run_python_with_importer(
        &archive,
        "import greet\nassert greet.GREETING == 'hello from fang', repr(greet.GREETING)\nprint('OK')",
    );
    let stdout = String::from_utf8_lossy(&out.stdout);
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        out.status.success() && stdout.contains("OK"),
        "stdout: {stdout}\nstderr: {stderr}"
    );
}

#[test]
fn test_file_attribute_is_fang_url() {
    let archive = build_archive("mymod", "X = 1");
    let out = run_python_with_importer(
        &archive,
        "import mymod\nassert mymod.__file__ == 'fang://app/mymod.pyc', repr(mymod.__file__)\nprint('OK')",
    );
    let stdout = String::from_utf8_lossy(&out.stdout);
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        out.status.success() && stdout.contains("OK"),
        "stdout: {stdout}\nstderr: {stderr}"
    );
}

#[test]
fn test_absent_module_falls_through() {
    // A module not in the archive should still be importable from the filesystem.
    let archive = build_archive("dummy_unused", "X = 1");
    let out = run_python_with_importer(
        &archive,
        "import os\nassert os.__file__ is not None\nprint('OK')",
    );
    let stdout = String::from_utf8_lossy(&out.stdout);
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        out.status.success() && stdout.contains("OK"),
        "stdout: {stdout}\nstderr: {stderr}"
    );
}

#[test]
fn test_find_spec_returns_none_for_unknown() {
    let archive = build_archive("dummy_unused2", "X = 1");
    let out = run_python_with_importer(
        &archive,
        r#"
import sys
importer = sys.meta_path[0]
result = importer.find_spec('nonexistent_module_xyz', None)
assert result is None, repr(result)
print('OK')
"#,
    );
    let stdout = String::from_utf8_lossy(&out.stdout);
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        out.status.success() && stdout.contains("OK"),
        "stdout: {stdout}\nstderr: {stderr}"
    );
}

#[test]
fn test_install_from_bytes_invalid_raises_value_error() {
    // No archive needed — test the error path directly.
    let dir = tempfile::tempdir().expect("tempdir");
    let so_src = extension_so();
    assert!(so_src.exists(), "extension .so not found");
    let so_dst = dir.path().join("fang_importer.so");
    std::fs::copy(&so_src, &so_dst).unwrap();

    let script = format!(
        r#"
import sys
sys.path.insert(0, r'{ext_dir}')
import fang_importer
try:
    fang_importer.install_from_bytes(b'not an archive')
    print('FAIL: no exception raised')
except ValueError as e:
    print('OK:', e)
"#,
        ext_dir = dir.path().display(),
    );

    let out = Command::new(python())
        .args(["-c", &script])
        .output()
        .expect("python subprocess");

    let stdout = String::from_utf8_lossy(&out.stdout);
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        out.status.success() && stdout.contains("OK"),
        "stdout: {stdout}\nstderr: {stderr}"
    );
}

#[test]
fn test_importlib_resources_reads_package_data() {
    let archive = build_archive_with_entries(
        &[("my_app.__init__", "")],
        &[
            ("app/my_app/x.txt", b"hello"),
            ("app/my_app/templates/default.txt", b"template"),
        ],
    );
    let out = run_python_with_importer(
        &archive,
        r#"
import importlib.resources as resources
import sys
import my_app

assert resources.files("my_app").joinpath("x.txt").read_text() == "hello"
assert resources.files("my_app").joinpath("templates/default.txt").read_text() == "template"
reader = sys.meta_path[0].get_resource_reader("my_app")
assert reader.is_resource("x.txt") is True
assert reader.is_resource("missing.txt") is False
assert "x.txt" in set(reader.contents())
try:
    reader.resource_path("x.txt")
except FileNotFoundError:
    pass
else:
    raise AssertionError("resource_path should raise FileNotFoundError")
print('OK')
"#,
    );
    let stdout = String::from_utf8_lossy(&out.stdout);
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        out.status.success() && stdout.contains("OK"),
        "stdout: {stdout}\nstderr: {stderr}"
    );
}

#[test]
fn test_importlib_metadata_reads_dist_info() {
    let archive = build_archive_with_entries(
        &[("dummy_unused3", "X = 1")],
        &[(
            "site-packages/typer-0.9.0.dist-info/METADATA",
            b"Metadata-Version: 2.1\nName: typer\nVersion: 0.9.0\n",
        )],
    );
    let out = run_python_with_importer(
        &archive,
        r#"
import importlib.metadata as metadata

assert metadata.version("typer") == "0.9.0"
dist = metadata.distribution("typer")
assert "Version: 0.9.0" in dist.read_text("METADATA")
assert dist.read_text("NOPE") is None
try:
    metadata.version("nonexistent-pkg")
except metadata.PackageNotFoundError:
    pass
else:
    raise AssertionError("missing package should raise PackageNotFoundError")
print('OK')
"#,
    );
    let stdout = String::from_utf8_lossy(&out.stdout);
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        out.status.success() && stdout.contains("OK"),
        "stdout: {stdout}\nstderr: {stderr}"
    );
}

#[test]
fn test_namespace_subpackage_without_init_imports() {
    let archive = build_archive_with_entries(
        &[
            ("transformerlab_cli.__init__", ""),
            ("transformerlab_cli.util.logo", "VALUE = 'logo'"),
        ],
        &[],
    );
    let out = run_python_with_importer(
        &archive,
        r#"
import transformerlab_cli.util
import transformerlab_cli.util.logo as logo

assert transformerlab_cli.util.__spec__.submodule_search_locations is not None
assert logo.VALUE == "logo"
print('OK')
"#,
    );
    let stdout = String::from_utf8_lossy(&out.stdout);
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        out.status.success() && stdout.contains("OK"),
        "stdout: {stdout}\nstderr: {stderr}"
    );
}
