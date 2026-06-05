use std::path::{Path, PathBuf};
use std::process::Command;

fn main() {
    println!("cargo:rerun-if-env-changed=FANG_PYTHON_VERSION");
    println!("cargo:rerun-if-env-changed=FANG_CPYTHON_CACHE");
    println!("cargo:rerun-if-env-changed=FANG_CPYTHON_TARBALL");
    println!("cargo:rerun-if-env-changed=FANG_ARCHIVE");
    println!("cargo:rerun-if-env-changed=PYO3_CONFIG_FILE");
    let manifest_dir = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let stdlib_script = manifest_dir
        .parent()
        .expect("fang-runtime must live below the workspace root")
        .join("scripts/build_stdlib_archive.py");
    println!("cargo:rerun-if-changed={}", stdlib_script.display());

    // When FANG_ARCHIVE is set, embed the archive as a binary section at link
    // time. This is how fang-cli produces output binaries and how the hello
    // world integration test works.
    embed_archive_section();

    let version = std::env::var("FANG_PYTHON_VERSION").unwrap_or_else(|_| {
        panic!(
            "\n\nFANG_PYTHON_VERSION must be set (e.g. FANG_PYTHON_VERSION=3.12, 3.12.3, or 3.12.3+20240415).\n\
             Set it in your environment or .cargo/config.toml.\n\n"
        )
    });
    assert_pyo3_config_matches(&version);

    let tarball = std::env::var("FANG_CPYTHON_TARBALL").unwrap_or_else(|_| {
        panic!(
            "\n\nFANG_CPYTHON_TARBALL must be set by build_runtime.sh.\n\
             Build fang-runtime with ./build_runtime.sh instead of invoking cargo directly.\n\n"
        )
    });
    let tarball = PathBuf::from(tarball);
    if !tarball.is_file() {
        panic!(
            "FANG_CPYTHON_TARBALL does not point to a file: {}",
            tarball.display()
        );
    }

    let out_dir = PathBuf::from(std::env::var("OUT_DIR").unwrap()).join("cpython");
    std::fs::create_dir_all(&out_dir).unwrap();

    build_runtime_stdlib(&stdlib_script, &tarball, &version, &out_dir);
    let extracted = extract_static_libs(&tarball, &version, &out_dir)
        .unwrap_or_else(|e| panic!("Failed to extract CPython static libraries: {e}"));

    println!(
        "cargo:rustc-link-search=native={}",
        extracted.lib_dir.display()
    );
    println!("cargo:rustc-link-lib=static={}", extracted.libpython_name);
    for dep in &extracted.dep_lib_names {
        println!("cargo:rustc-link-lib=static={dep}");
    }

    emit_system_libs();
}

struct ExtractedLibs {
    lib_dir: PathBuf,
    libpython_name: String,
    dep_lib_names: Vec<String>,
}

fn build_runtime_stdlib(script: &Path, tarball: &Path, version: &str, out_dir: &Path) {
    let stdlib_out = out_dir.join("runtime_stdlib.fang");
    let frozen_out = out_dir.join("frozen_bootstrap.rs");
    let status = Command::new("python3")
        .arg(script)
        .arg("--tarball")
        .arg(tarball)
        .arg("--python-version")
        .arg(version)
        .arg("--out")
        .arg(&stdlib_out)
        .arg("--frozen-out")
        .arg(&frozen_out)
        .status()
        .unwrap_or_else(|e| panic!("failed to run {}: {e}", script.display()));
    assert!(
        status.success(),
        "scripts/build_stdlib_archive.py failed with status {status}"
    );
}

fn extract_static_libs(
    tarball: &Path,
    version: &str,
    out_dir: &Path,
) -> Result<ExtractedLibs, String> {
    let lib_dir = out_dir.join("lib");
    let hacl_dir = out_dir.join("hacl");
    if lib_dir.exists() {
        std::fs::remove_dir_all(&lib_dir).map_err(|e| e.to_string())?;
    }
    if hacl_dir.exists() {
        std::fs::remove_dir_all(&hacl_dir).map_err(|e| e.to_string())?;
    }
    std::fs::create_dir_all(&lib_dir).map_err(|e| e.to_string())?;
    std::fs::create_dir_all(&hacl_dir).map_err(|e| e.to_string())?;

    let series = python_minor_version(version).ok_or_else(|| {
        format!("FANG_PYTHON_VERSION must start with major.minor, got {version:?}")
    })?;
    let libpython_name = format!("python{series}");
    let libfile = format!("lib{libpython_name}.a");
    let members = list_tar_members(tarball)?;

    let libpython_member = members
        .iter()
        .filter(|member| member.ends_with(&format!("/{libfile}")))
        .max_by_key(|member| if member.contains("/config-") { 1 } else { 0 })
        .ok_or_else(|| format!("did not find {libfile} in {}", tarball.display()))?;
    extract_member_basename(tarball, libpython_member, &lib_dir)?;

    let mut dep_lib_names = Vec::new();
    for member in &members {
        let Some(fname) = member.strip_prefix("python/build/lib/") else {
            continue;
        };
        if !fname.ends_with(".a") {
            continue;
        }
        if fname.starts_with("libitcl")
            || fname.starts_with("libtkstub")
            || fname.starts_with("libtclstub")
            || fname.starts_with("libclang_rt")
        {
            continue;
        }
        extract_member_basename(tarball, member, &lib_dir)?;
        if let Some(stem) = fname.strip_prefix("lib").and_then(|s| s.strip_suffix(".a")) {
            dep_lib_names.push(stem.to_string());
        }
    }

    let mut hacl_objects = Vec::new();
    for member in &members {
        let Some(fname) = member.strip_prefix("python/build/Modules/_hacl/") else {
            continue;
        };
        if !fname.ends_with(".o") || fname.contains('/') {
            continue;
        }
        extract_member_basename(tarball, member, &hacl_dir)?;
        hacl_objects.push(hacl_dir.join(fname));
    }

    if !hacl_objects.is_empty() {
        let hacl_archive = lib_dir.join("libpython_hacl.a");
        let status = Command::new("ar")
            .arg("rcs")
            .arg(&hacl_archive)
            .args(&hacl_objects)
            .status()
            .map_err(|e| format!("ar failed to run: {e}"))?;
        if !status.success() {
            return Err("ar rcs for libpython_hacl.a failed".into());
        }
        dep_lib_names.push("python_hacl".into());
    }

    Ok(ExtractedLibs {
        lib_dir,
        libpython_name,
        dep_lib_names,
    })
}

fn list_tar_members(tarball: &Path) -> Result<Vec<String>, String> {
    let output = Command::new("tar")
        .arg("--zstd")
        .arg("-tf")
        .arg(tarball)
        .output()
        .map_err(|e| format!("tar -tf failed to run: {e}"))?;
    if !output.status.success() {
        return Err(format!(
            "tar -tf failed for {}: {}",
            tarball.display(),
            String::from_utf8_lossy(&output.stderr)
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout)
        .lines()
        .map(str::to_string)
        .collect())
}

fn extract_member_basename(tarball: &Path, member: &str, dest_dir: &Path) -> Result<(), String> {
    let strip_components = member.split('/').count().saturating_sub(1).to_string();
    let dest = member
        .rsplit('/')
        .next()
        .map(|name| dest_dir.join(name))
        .ok_or_else(|| format!("invalid tar member path: {member}"))?;
    if dest.exists() {
        std::fs::remove_file(&dest).map_err(|e| e.to_string())?;
    }
    let output = Command::new("tar")
        .arg("-x")
        .arg("--zstd")
        .arg("--strip-components")
        .arg(&strip_components)
        .arg("-f")
        .arg(tarball)
        .arg("-C")
        .arg(dest_dir)
        .arg(member)
        .env("LC_ALL", "C")
        .env("LANG", "C")
        .output()
        .map_err(|e| format!("tar extraction failed to run: {e}"))?;
    if !output.status.success() {
        if dest.metadata().map(|m| m.len() > 0).unwrap_or(false) {
            return Ok(());
        }
        return Err(format!(
            "tar extraction failed for member {member} from {} into {}: {}",
            tarball.display(),
            dest_dir.display(),
            String::from_utf8_lossy(&output.stderr)
        ));
    }
    Ok(())
}

fn assert_pyo3_config_matches(version: &str) {
    let Some(fang_minor) = python_minor_version(version) else {
        panic!("FANG_PYTHON_VERSION must start with major.minor, got {version:?}");
    };

    let Ok(config_path) = std::env::var("PYO3_CONFIG_FILE") else {
        panic!(
            "\n\nPYO3_CONFIG_FILE must be set to a full-ABI PyO3 config matching \
             FANG_PYTHON_VERSION={version}. The repository default is \
             .cargo/pyo3-cpython-3.12.cfg.\n\n"
        );
    };

    let config = std::fs::read_to_string(&config_path).unwrap_or_else(|e| {
        panic!("failed to read PYO3_CONFIG_FILE={config_path:?}: {e}");
    });
    let pyo3_minor = config
        .lines()
        .find_map(|line| line.strip_prefix("version="))
        .and_then(python_minor_version)
        .unwrap_or_else(|| {
            panic!("PYO3_CONFIG_FILE={config_path:?} must contain version=major.minor")
        });
    let abi3 = config
        .lines()
        .find_map(|line| line.strip_prefix("abi3="))
        .unwrap_or("false");

    if abi3 != "false" {
        panic!("PYO3_CONFIG_FILE={config_path:?} must set abi3=false for fang-runtime");
    }

    if pyo3_minor != fang_minor {
        panic!(
            "\n\nPYO3_CONFIG_FILE={config_path:?} configures Python {pyo3_minor}, \
             but FANG_PYTHON_VERSION={version:?} requires Python {fang_minor}. \
             Update the PyO3 config before building fang-runtime.\n\n"
        );
    }
}

fn python_minor_version(version: &str) -> Option<String> {
    let base = version.split_once('+').map(|(v, _)| v).unwrap_or(version);
    let mut parts = base.splitn(3, '.');
    let major = parts.next()?;
    let minor = parts.next()?;
    Some(format!("{major}.{minor}"))
}

#[cfg(target_os = "linux")]
fn emit_system_libs() {
    for lib in ["pthread", "dl", "util", "m", "z"] {
        println!("cargo:rustc-link-lib={lib}");
    }
}

#[cfg(target_os = "macos")]
fn emit_system_libs() {
    for framework in [
        "AppKit",
        "ApplicationServices",
        "Carbon",
        "Cocoa",
        "CoreFoundation",
        "CoreGraphics",
        "CoreServices",
        "Foundation",
        "IOKit",
        "QuartzCore",
        "SystemConfiguration",
    ] {
        println!("cargo:rustc-link-lib=framework={framework}");
    }
    for lib in ["edit", "ncurses", "panel", "resolv", "z"] {
        println!("cargo:rustc-link-lib={lib}");
    }
    // Required for Objective-C categories in Tk's Aqua backend.
    println!("cargo:rustc-link-arg=-ObjC");
    // Export all global symbols (including statically-linked libpython) into the
    // dynamic symbol table so that dlopen'd C extensions (e.g. numpy) can resolve
    // Python C API symbols via the flat namespace.
    println!("cargo:rustc-link-arg=-Wl,-export_dynamic");
}

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
fn emit_system_libs() {
    compile_error!("fang-runtime only supports Linux and macOS");
}

#[cfg(target_os = "macos")]
fn embed_archive_section() {
    if let Ok(path) = std::env::var("FANG_ARCHIVE") {
        let abs = std::path::PathBuf::from(&path)
            .canonicalize()
            .unwrap_or_else(|e| panic!("FANG_ARCHIVE path {path:?} invalid: {e}"));
        println!(
            "cargo:rustc-link-arg=-Wl,-sectcreate,__FANG,__assets,{}",
            abs.display()
        );
    }
}

#[cfg(target_os = "linux")]
fn embed_archive_section() {
    if let Ok(path) = std::env::var("FANG_ARCHIVE") {
        let abs = std::path::PathBuf::from(&path)
            .canonicalize()
            .unwrap_or_else(|e| panic!("FANG_ARCHIVE path {path:?} invalid: {e}"));
        // ld: create a relocatable object with the section, then link it in.
        let out_dir = PathBuf::from(std::env::var("OUT_DIR").unwrap());
        let obj = out_dir.join("fang_assets.o");
        let raw_obj = out_dir.join("fang_assets_raw.o");
        let status = std::process::Command::new("ld")
            .args([
                "-r",
                "--format=binary",
                abs.to_str().unwrap(),
                "--format=default",
                "-o",
                raw_obj.to_str().unwrap(),
            ])
            .status()
            .unwrap_or_else(|e| panic!("ld failed: {e}"));
        assert!(status.success(), "ld failed to create fang_assets.o");

        let status = std::process::Command::new("objcopy")
            .arg("--rename-section")
            .arg(".data=fang_assets,alloc,load,readonly,data,contents")
            .arg(&raw_obj)
            .arg(&obj)
            .status()
            .unwrap_or_else(|e| panic!("objcopy failed: {e}"));
        assert!(status.success(), "objcopy failed to create fang_assets.o");
        println!("cargo:rustc-link-arg={}", obj.display());
    }
}

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
fn embed_archive_section() {}
