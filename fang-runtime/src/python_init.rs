use crate::frozen::install_frozen_bootstrap;
use fang_importer::install_fang_importer;
use fang_pack::{Archive, Meta};
use libc::wchar_t;
use pyo3_ffi::*;
use std::collections::HashMap;
use std::ffi::{CStr, CString};
use std::path::PathBuf;
use std::ptr;
extern crate libc;

pub unsafe fn init_cpython(program_name: &str) -> Result<(), String> {
    // Freeze encodings before init so CPython can resolve the
    // filesystem codec without needing /install on the host filesystem.
    install_frozen_bootstrap();

    let mut config = std::mem::zeroed::<PyConfig>();
    PyConfig_InitIsolatedConfig(&mut config);

    let result = init_cpython_with_config(&mut config, program_name);
    PyConfig_Clear(&mut config);
    result
}

pub unsafe fn validate_version(meta_version: &str) -> Result<(), String> {
    let meta_series = meta_version
        .split_once('+')
        .map(|(v, _)| v)
        .unwrap_or(meta_version);
    let mut parts = meta_series.splitn(3, '.');
    let meta_major: i32 = parts.next().and_then(|s| s.parse().ok()).unwrap_or(-1);
    let meta_minor: i32 = parts.next().and_then(|s| s.parse().ok()).unwrap_or(-1);

    // Read linked major/minor via sys.version_info (always available post-init)
    let sys = PyImport_ImportModule(c"sys".as_ptr());
    if sys.is_null() {
        return Err("failed to import sys for version check".into());
    }
    let vi = PyObject_GetAttrString(sys, c"version_info".as_ptr());
    Py_DECREF(sys);
    if vi.is_null() {
        return Err("failed to get sys.version_info".into());
    }

    let linked_major = PyLong_AsLong(PySequence_GetItem(vi, 0)) as i32;
    let linked_minor = PyLong_AsLong(PySequence_GetItem(vi, 1)) as i32;
    Py_DECREF(vi);

    if meta_major != linked_major || meta_minor != linked_minor {
        return Err(format!(
            "fang: archive built for Python {meta_major}.{meta_minor} \
             but runtime links Python {linked_major}.{linked_minor}"
        ));
    }
    Ok(())
}

/// Pre-warm modules that must survive in sys.modules.
/// `runpy` is not frozen in CPython 3.12, so it is imported from the Fang
/// archive after FangImporter is installed.
pub unsafe fn prewarm_stdlib_modules() -> Result<(), String> {
    for name in [c"importlib.util", c"runpy"] {
        let module = PyImport_ImportModule(name.as_ptr());
        if module.is_null() {
            let name_str = name.to_str().unwrap_or("?");
            let msg = fetch_exception_string().unwrap_or_else(|| "unknown error".into());
            PyErr_Clear();
            return Err(format!("fang: failed to pre-warm {name_str}: {msg}"));
        }
        Py_DECREF(module);
    }
    Ok(())
}

unsafe fn init_cpython_with_config(
    config: *mut PyConfig,
    program_name: &str,
) -> Result<(), String> {
    (*config).site_import = 0;
    (*config).write_bytecode = 0;
    (*config).use_environment = 0;
    (*config).user_site_directory = 0;
    (*config).install_signal_handlers = 0;
    (*config).pathconfig_warnings = 0;
    (*config).use_frozen_modules = 1;

    // Own all path configuration up front so CPython does not run getpath.py's
    // stdlib landmark search against the host filesystem.
    (*config).module_search_paths_set = 1;

    let program_name_c = CString::new(program_name)
        .map_err(|_| "program name contains an interior NUL byte".to_string())?;
    check_status(
        PyConfig_SetBytesString(
            config,
            ptr::addr_of_mut!((*config).program_name),
            program_name_c.as_ptr(),
        ),
        "PyConfig_SetBytesString(program_name)",
    )?;

    let executable = std::env::current_exe()
        .map(|path| path.to_string_lossy().into_owned())
        .unwrap_or_else(|_| program_name.to_owned());
    let executable = wide_nul(&executable);
    set_config_string(
        config,
        ptr::addr_of_mut!((*config).executable),
        &executable,
        "executable",
    )?;
    set_config_string(
        config,
        ptr::addr_of_mut!((*config).base_executable),
        &executable,
        "base_executable",
    )?;

    let fang_prefix = wide_nul("fang://");
    let utf8 = wide_nul("utf_8");
    let surrogateescape = wide_nul("surrogateescape");
    set_config_string(
        config,
        ptr::addr_of_mut!((*config).filesystem_encoding),
        &utf8,
        "filesystem_encoding",
    )?;
    set_config_string(
        config,
        ptr::addr_of_mut!((*config).filesystem_errors),
        &surrogateescape,
        "filesystem_errors",
    )?;
    set_config_string(
        config,
        ptr::addr_of_mut!((*config).stdio_encoding),
        &utf8,
        "stdio_encoding",
    )?;
    set_config_string(
        config,
        ptr::addr_of_mut!((*config).stdio_errors),
        &surrogateescape,
        "stdio_errors",
    )?;

    set_config_string(
        config,
        ptr::addr_of_mut!((*config).home),
        &fang_prefix,
        "home",
    )?;
    set_config_string(
        config,
        ptr::addr_of_mut!((*config).prefix),
        &fang_prefix,
        "prefix",
    )?;
    set_config_string(
        config,
        ptr::addr_of_mut!((*config).base_prefix),
        &fang_prefix,
        "base_prefix",
    )?;
    set_config_string(
        config,
        ptr::addr_of_mut!((*config).exec_prefix),
        &fang_prefix,
        "exec_prefix",
    )?;
    set_config_string(
        config,
        ptr::addr_of_mut!((*config).base_exec_prefix),
        &fang_prefix,
        "base_exec_prefix",
    )?;
    set_config_string(
        config,
        ptr::addr_of_mut!((*config).stdlib_dir),
        &fang_prefix,
        "stdlib_dir",
    )?;

    check_status(Py_InitializeFromConfig(config), "Py_InitializeFromConfig")?;

    if Py_IsInitialized() == 0 {
        return Err("Py_InitializeFromConfig returned without initializing the interpreter".into());
    }

    Ok(())
}

unsafe fn set_config_string(
    config: *mut PyConfig,
    field: *mut *mut wchar_t,
    value: &[wchar_t],
    field_name: &str,
) -> Result<(), String> {
    check_status(
        PyConfig_SetString(config, field, value.as_ptr()),
        &format!("PyConfig_SetString({field_name})"),
    )
}

fn wide_nul(value: &str) -> Vec<wchar_t> {
    value
        .chars()
        .map(|ch| ch as u32 as wchar_t)
        .chain(std::iter::once(0))
        .collect()
}

unsafe fn check_status(status: PyStatus, context: &str) -> Result<(), String> {
    if PyStatus_Exception(status) == 0 {
        return Ok(());
    }

    let message = if !status.err_msg.is_null() {
        CStr::from_ptr(status.err_msg)
            .to_string_lossy()
            .into_owned()
    } else if PyStatus_IsExit(status) != 0 {
        format!("Python initialization requested exit {}", status.exitcode)
    } else {
        "unknown Python initialization error".to_string()
    };
    Err(format!("{context}: {message}"))
}

pub unsafe fn setup_meta_path(
    archive: *const Archive,
    runtime_stdlib: *const Archive,
    meta: &Meta,
) -> Result<(), String> {
    prepare_native_libs(archive, meta)?;

    // Set sys.prefix and sys.exec_prefix to a synthetic non-filesystem path.
    let fang_prefix = PyUnicode_FromString(c"fang://".as_ptr());
    if fang_prefix.is_null() {
        return Err("failed to create fang:// prefix string".into());
    }
    PySys_SetObject(c"prefix".as_ptr(), fang_prefix);
    PySys_SetObject(c"exec_prefix".as_ptr(), fang_prefix);
    Py_DECREF(fang_prefix);

    // Set sys.path to empty list.
    let empty = PyList_New(0);
    if empty.is_null() {
        return Err("failed to create empty sys.path list".into());
    }
    PySys_SetObject(c"path".as_ptr(), empty);
    Py_DECREF(empty);

    // Remove PathFinder from sys.meta_path.
    let sys = PyImport_ImportModule(c"sys".as_ptr());
    if sys.is_null() {
        return Err("failed to import sys".into());
    }
    let meta_path = PyObject_GetAttrString(sys, c"meta_path".as_ptr());
    Py_DECREF(sys);
    if meta_path.is_null() {
        return Err("failed to get sys.meta_path".into());
    }
    remove_path_finder(meta_path);
    Py_DECREF(meta_path);

    // Leak the extension index for process lifetime.
    let ext_index =
        Box::into_raw(Box::new(meta.extensions.clone())) as *const HashMap<String, String>;
    let rtld_global = meta.rtld_global;
    let cache_dir = compute_cache_dir(meta);

    // Install FangImporter at the front of sys.meta_path.
    install_fang_importer(archive, runtime_stdlib, ext_index, rtld_global, cache_dir)
        .map_err(|e| format!("install_fang_importer: {e}"))
}

fn prepare_native_libs(archive: *const Archive, meta: &Meta) -> Result<(), String> {
    if meta.native_libs.is_empty() {
        return Ok(());
    }
    #[cfg(target_os = "linux")]
    unsafe {
        preload_native_libs_linux(archive, meta)
    }
    #[cfg(target_os = "macos")]
    unsafe {
        let cache_dir = compute_cache_dir(meta);
        extract_native_libs_macos(archive, meta, cache_dir)
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos")))]
    {
        let _ = archive;
        Err("native library bundles are only supported on Linux and macOS".into())
    }
}

#[cfg(target_os = "linux")]
unsafe fn preload_native_libs_linux(archive: *const Archive, meta: &Meta) -> Result<(), String> {
    use std::io::Write as _;
    use std::os::unix::io::{FromRawFd, IntoRawFd};

    let archive = &*archive;
    for lib_path in &meta.native_libs {
        let blob = match archive.get_verified(lib_path) {
            Some(Ok(blob)) => blob,
            Some(Err(err)) => {
                return Err(format!("failed to verify native library {lib_path}: {err}"));
            }
            None => return Err(format!("native library not found in archive: {lib_path}")),
        };

        let fd_name = lib_path.rsplit('/').next().unwrap_or("fang-native-lib");
        let fd_name =
            CString::new(fd_name).unwrap_or_else(|_| CString::new("fang-native-lib").unwrap());
        let fd = libc::memfd_create(fd_name.as_ptr(), libc::MFD_CLOEXEC);
        if fd < 0 {
            return Err(format!("memfd_create failed for native library {lib_path}"));
        }

        {
            let mut file = std::fs::File::from_raw_fd(fd);
            if let Err(err) = file.write_all(&blob) {
                return Err(format!(
                    "failed to write native library {lib_path} to memfd: {err}"
                ));
            }
            let _ = file.flush();
            let _ = file.into_raw_fd();
        }

        let proc_path = CString::new(format!("/proc/self/fd/{fd}")).unwrap();
        let handle = libc::dlopen(proc_path.as_ptr(), libc::RTLD_NOW | libc::RTLD_GLOBAL);
        libc::close(fd);
        if handle.is_null() {
            let dlerr = libc::dlerror();
            let detail = if dlerr.is_null() {
                "unknown error".to_owned()
            } else {
                CStr::from_ptr(dlerr).to_string_lossy().into_owned()
            };
            return Err(format!(
                "dlopen failed for native library {lib_path}: {detail}"
            ));
        }
    }
    Ok(())
}

#[cfg(target_os = "macos")]
unsafe fn extract_native_libs_macos(
    archive: *const Archive,
    meta: &Meta,
    cache_dir: *const PathBuf,
) -> Result<(), String> {
    if cache_dir.is_null() {
        return Err("native library cache directory is unavailable".into());
    }
    let archive = &*archive;
    let base = &*cache_dir;
    for lib_path in &meta.native_libs {
        let rel = lib_path
            .strip_prefix("native-libs/")
            .or_else(|| lib_path.strip_prefix("extensions/"))
            .unwrap_or(lib_path);
        if fang_pack::has_traversal(rel) {
            return Err(format!(
                "path traversal detected in native library archive path: {lib_path}"
            ));
        }
        let blob = match archive.get_verified(lib_path) {
            Some(Ok(blob)) => blob,
            Some(Err(err)) => {
                return Err(format!("failed to verify native library {lib_path}: {err}"));
            }
            None => return Err(format!("native library not found in archive: {lib_path}")),
        };
        let cache_path = base.join(rel);
        let expected_hash = *blake3::hash(&blob).as_bytes();
        if cache_path.exists() {
            match std::fs::read(&cache_path) {
                Ok(cached_bytes) if *blake3::hash(&cached_bytes).as_bytes() == expected_hash => {
                    continue;
                }
                _ => {
                    let _ = std::fs::remove_file(&cache_path);
                }
            }
        }
        write_cache_atomic(&cache_path, &blob).map_err(|err| {
            format!(
                "failed to write cached native library {}: {}",
                lib_path, err
            )
        })?;
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn write_cache_atomic(cache_path: &std::path::Path, blob: &[u8]) -> std::io::Result<()> {
    use std::io::Write as _;
    use std::os::unix::fs::OpenOptionsExt;

    let parent = cache_path.parent().ok_or_else(|| {
        std::io::Error::new(std::io::ErrorKind::InvalidInput, "cache path has no parent")
    })?;
    std::fs::create_dir_all(parent)?;
    let temp_path = parent.join(format!(".fang-native-tmp-{}", std::process::id()));
    let mut f = std::fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(&temp_path)?;
    if let Err(err) = f.write_all(blob) {
        let _ = std::fs::remove_file(&temp_path);
        return Err(err);
    }
    drop(f);
    std::fs::rename(&temp_path, cache_path)
}

pub(crate) unsafe fn remove_path_finder(meta_path: *mut PyObject) {
    let len = PyList_Size(meta_path);
    for i in 0..len {
        let item = PyList_GetItem(meta_path, i); // borrowed ref
        if item.is_null() {
            continue;
        }
        let type_obj = Py_TYPE(item) as *mut PyObject;
        let name_obj = PyObject_GetAttrString(type_obj, c"__name__".as_ptr());
        if name_obj.is_null() {
            continue;
        }
        let mut size: Py_ssize_t = 0;
        let ptr = PyUnicode_AsUTF8AndSize(name_obj, &mut size);
        Py_DECREF(name_obj);
        if ptr.is_null() {
            continue;
        }
        let name_bytes = std::slice::from_raw_parts(ptr as *const u8, size as usize);
        if name_bytes == b"PathFinder" {
            PySequence_DelItem(meta_path, i);
            return;
        }
    }
    // PathFinder not present is fine — isolated init may omit it
}

/// Returns true if `name` is safe to embed in a filesystem path component.
/// Permits alphanumeric, dot, dash, underscore only.
fn is_safe_path_component(s: &str) -> bool {
    !s.is_empty()
        && s.chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '_'))
}

/// Returns true if `ts` contains only ISO 8601-compatible characters.
fn is_safe_timestamp(s: &str) -> bool {
    !s.is_empty()
        && s.chars()
            .all(|c| c.is_ascii_digit() || matches!(c, 'T' | ':' | '.' | 'Z' | '+' | '-'))
}

/// Compute the macOS dylib cache directory for this binary.
///
/// Returns a heap-allocated PathBuf pointer on macOS (leaked for process lifetime),
/// or null on Linux (cache dir unused).
pub(crate) fn compute_cache_dir(meta: &Meta) -> *const PathBuf {
    #[cfg(target_os = "macos")]
    {
        // Validate project_name against a safe character allowlist; fall back to "fang-app"
        // if empty, contains path separators, or any character outside [a-zA-Z0-9._-].
        let app_name = if is_safe_path_component(&meta.project_name) {
            meta.project_name.as_str()
        } else {
            "fang-app"
        };

        // Validate build_timestamp to ISO 8601 character set; fall back to "unknown".
        // Apply colon→dash replacement only after the allowlist check passes.
        let timestamp_safe = if is_safe_timestamp(&meta.build_timestamp) {
            meta.build_timestamp.replace(':', "-")
        } else {
            "unknown".to_string()
        };

        let home = std::env::var("HOME").unwrap_or_else(|_| "/tmp".to_string());
        let cache_dir = PathBuf::from(home)
            .join("Library/Caches")
            .join(app_name)
            .join(timestamp_safe);
        // Create eagerly so extensions can be written there on first run.
        let _ = std::fs::create_dir_all(&cache_dir);
        Box::into_raw(Box::new(cache_dir)) as *const PathBuf
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = meta;
        std::ptr::null()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use fang_pack::Meta;
    use std::collections::HashMap;

    fn make_meta(project_name: &str, build_timestamp: &str) -> Meta {
        Meta {
            python_version: "3.12.0".into(),
            entry_point: "app.__main__".into(),
            entry_callable: None,
            platform: "macos-arm64".into(),
            build_timestamp: build_timestamp.into(),
            project_name: project_name.into(),
            extensions: HashMap::new(),
            native_libs: Vec::new(),
            rtld_global: true,
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn normal_project_name_used_as_is() {
        let meta = make_meta("my-app", "2024-01-15T10:30:00Z");
        let ptr = compute_cache_dir(&meta);
        assert!(!ptr.is_null());
        let path = unsafe { &*ptr };
        assert!(path.to_str().unwrap().contains("my-app"));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn traversal_in_project_name_falls_back() {
        let meta = make_meta("../../.ssh", "2024-01-15T10:30:00Z");
        let ptr = compute_cache_dir(&meta);
        assert!(!ptr.is_null());
        let path = unsafe { &*ptr };
        let s = path.to_str().unwrap();
        assert!(
            s.contains("fang-app"),
            "expected fang-app fallback, got: {s}"
        );
        assert!(!s.contains(".ssh"));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn unicode_project_name_falls_back() {
        let meta = make_meta("我的应用", "2024-01-15T10:30:00Z");
        let ptr = compute_cache_dir(&meta);
        assert!(!ptr.is_null());
        let path = unsafe { &*ptr };
        assert!(path.to_str().unwrap().contains("fang-app"));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn traversal_in_timestamp_falls_back() {
        let meta = make_meta("my-app", "../../etc/cron.d");
        let ptr = compute_cache_dir(&meta);
        assert!(!ptr.is_null());
        let path = unsafe { &*ptr };
        let s = path.to_str().unwrap();
        assert!(s.contains("unknown"), "expected unknown fallback, got: {s}");
        assert!(!s.contains("cron"));
    }
}

unsafe fn fetch_exception_string() -> Option<String> {
    let pvalue = PyErr_GetRaisedException();
    if pvalue.is_null() {
        return None;
    }
    let s_obj = PyObject_Str(pvalue);
    Py_XDECREF(pvalue);
    if s_obj.is_null() {
        return None;
    }
    let mut size: Py_ssize_t = 0;
    let ptr = PyUnicode_AsUTF8AndSize(s_obj, &mut size);
    let result = if ptr.is_null() {
        None
    } else {
        let bytes = std::slice::from_raw_parts(ptr as *const u8, size as usize);
        std::str::from_utf8(bytes).ok().map(|s| s.to_owned())
    };
    Py_DECREF(s_obj);
    result
}
