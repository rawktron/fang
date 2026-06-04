use std::collections::{BTreeSet, HashMap};
use std::path::PathBuf;

use crate::paths::{module_name_to_paths, module_name_to_prefixed_paths};
use fang_pack::Archive;
use pyo3_ffi::*;
use std::ffi::CString;
use std::os::raw::{c_char, c_int, c_uint, c_void};
use std::ptr;

// pyo3-ffi excludes marshal from the limited API re-export, but the function
// IS in CPython's limited ABI since 3.4. Declare it directly.
extern "C" {
    fn PyMarshal_ReadObjectFromString(data: *const c_char, len: Py_ssize_t) -> *mut PyObject;
}

// The initialized type object (set once by init_type)
static mut FANG_IMPORTER_TYPE_OBJ: *mut PyObject = ptr::null_mut();
static mut FANG_RESOURCE_READER_TYPE_OBJ: *mut PyObject = ptr::null_mut();
static mut FANG_DISTRIBUTION_TYPE_OBJ: *mut PyObject = ptr::null_mut();

// ---------------------------------------------------------------------------
// FangImporterObject
// ---------------------------------------------------------------------------

#[repr(C)]
pub struct FangImporterObject {
    pub ob_base: PyObject,
    pub archive: *const Archive,
    pub stdlib_archive: *const Archive,
    /// module_name → archive_path index from meta.json (heap-allocated, process lifetime)
    pub ext_index: *const HashMap<String, String>,
    /// RTLD_GLOBAL (true) or RTLD_LOCAL (false) for dlopen
    pub rtld_global: bool,
    /// macOS cache dir: ~/Library/Caches/<app>/<build_timestamp>/ (null on Linux)
    pub cache_dir: *const PathBuf,
}

#[repr(C)]
pub struct ArchiveResourceReaderObject {
    pub ob_base: PyObject,
    pub archive: *const Archive,
    pub package_prefix: *const String,
}

#[repr(C)]
pub struct ArchiveDistributionObject {
    pub ob_base: PyObject,
    pub archive: *const Archive,
    pub dist_prefix: *const String,
    pub normalized_name: *const String,
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Convert a PyUnicode object to an owned String.
unsafe fn pyunicode_to_string(uni: *mut PyObject) -> Option<String> {
    let mut size: Py_ssize_t = 0;
    let ptr = PyUnicode_AsUTF8AndSize(uni, &mut size);
    if ptr.is_null() {
        return None;
    }
    let slice = std::slice::from_raw_parts(ptr as *const u8, size as usize);
    std::str::from_utf8(slice).ok().map(|s| s.to_owned())
}

unsafe fn py_none() -> *mut PyObject {
    Py_IncRef(Py_None());
    Py_None()
}

unsafe fn py_string(value: &str) -> *mut PyObject {
    PyUnicode_FromStringAndSize(value.as_ptr() as *const c_char, value.len() as isize)
}

/// Derive the `PyInit_<leaf>` symbol name from an extension archive path.
///
/// `extensions/numpy/core/_multiarray_umath.cpython-312-x86_64-linux-gnu.so`
///   → `PyInit__multiarray_umath`
pub(crate) fn init_symbol_from_archive_path(archive_path: &str) -> String {
    let filename = archive_path.rsplit('/').next().unwrap_or(archive_path);
    let stem = filename.split('.').next().unwrap_or(filename);
    format!("PyInit_{stem}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn init_symbol_nested_extension() {
        assert_eq!(
            init_symbol_from_archive_path(
                "extensions/numpy/core/_multiarray_umath.cpython-312-x86_64-linux-gnu.so"
            ),
            "PyInit__multiarray_umath"
        );
    }

    #[test]
    fn init_symbol_top_level() {
        assert_eq!(
            init_symbol_from_archive_path("extensions/_csv.cpython-312-x86_64-linux-gnu.so"),
            "PyInit__csv"
        );
    }

    #[test]
    fn init_symbol_no_platform_tag() {
        assert_eq!(
            init_symbol_from_archive_path("extensions/mymodule.so"),
            "PyInit_mymodule"
        );
    }

    #[test]
    fn runtime_stdlib_match_finds_module_and_package_source() {
        let mut b = fang_pack::ArchiveBuilder::new();
        b.add("stdlib/os.py", b"NAME = 'posix'\n").unwrap();
        b.add("stdlib/urllib/__init__.py", b"").unwrap();
        b.set_meta(fang_pack::Meta {
            python_version: "3.12.3+20240415".into(),
            entry_point: "__stdlib__".into(),
            entry_callable: None,
            platform: "macos-arm64".into(),
            build_timestamp: "runtime-stdlib".into(),
            project_name: "fang-runtime-stdlib".into(),
            extensions: std::collections::HashMap::new(),
            native_libs: Vec::new(),
            rtld_global: true,
        });
        let bytes = b.build().unwrap();
        let archive = fang_pack::Archive::from_bytes(&bytes).unwrap();

        assert_eq!(
            runtime_stdlib_match(&archive, "os"),
            Some(("stdlib/os.py".into(), false))
        );
        assert_eq!(
            runtime_stdlib_match(&archive, "urllib"),
            Some(("stdlib/urllib/__init__.py".into(), true))
        );
        assert_eq!(runtime_stdlib_match(&archive, "missing"), None);
    }
}

/// dlopen flags based on rtld_global setting.
fn dlopen_flags(rtld_global: bool) -> libc::c_int {
    if rtld_global {
        libc::RTLD_NOW | libc::RTLD_GLOBAL
    } else {
        libc::RTLD_NOW | libc::RTLD_LOCAL
    }
}

/// Call dlsym for the init function and invoke it, returning the module object.
///
/// Handles both single-phase init (PyInit_ returns a PyModule) and multi-phase
/// init/PEP 451 (PyInit_ returns a PyModuleDef). For multi-phase init, calls
/// PyModule_FromDefAndSpec to create the actual module from the def + spec.
/// Sets a Python ImportError on failure.
unsafe fn call_init_fn(
    handle: *mut libc::c_void,
    init_sym: &str,
    archive_path: &str,
    spec: *mut PyObject,
) -> *mut PyObject {
    let sym_cstr = match CString::new(init_sym) {
        Ok(s) => s,
        Err(_) => {
            let msg = CString::new(format!(
                "invalid init symbol name for extension: {}",
                archive_path
            ))
            .unwrap();
            PyErr_SetString(PyExc_ImportError, msg.as_ptr());
            return ptr::null_mut();
        }
    };

    let sym = libc::dlsym(handle, sym_cstr.as_ptr());
    if sym.is_null() {
        let dlerr = libc::dlerror();
        let detail = if dlerr.is_null() {
            "unknown error".to_owned()
        } else {
            std::ffi::CStr::from_ptr(dlerr)
                .to_string_lossy()
                .into_owned()
        };
        let msg = CString::new(format!(
            "symbol {} not found in {}: {}",
            init_sym, archive_path, detail
        ))
        .unwrap();
        PyErr_SetString(PyExc_ImportError, msg.as_ptr());
        return ptr::null_mut();
    }

    let init_fn: unsafe extern "C" fn() -> *mut PyObject = std::mem::transmute(sym);
    let result = init_fn();
    if result.is_null() {
        return ptr::null_mut();
    }

    if PyModule_Check(result) != 0 {
        // Single-phase init: PyInit_ already returned the fully-created module.
        result
    } else {
        // Multi-phase init (PEP 451): PyInit_ returned a PyModuleDef.
        // Do NOT Py_DECREF the def: PyModuleDef objects are typically static
        // variables in the extension's data segment. After PyType_Ready,
        // tp_dealloc is inherited from object and would call free() on static
        // memory → malloc abort. The module holds md_def and needs it alive.
        PyModule_FromDefAndSpec(result as *mut PyModuleDef, spec)
    }
}

// ---------------------------------------------------------------------------
// Platform-specific extension loading
// ---------------------------------------------------------------------------

#[cfg(target_os = "linux")]
unsafe fn load_extension(
    blob: &[u8],
    _expected_hash: [u8; 32],
    archive_path: &str,
    module_name: &str,
    rtld_global: bool,
    _cache_dir: *const PathBuf,
    spec: *mut PyObject,
    _archive: &Archive,
) -> *mut PyObject {
    use std::io::Write as _;
    use std::os::unix::io::FromRawFd;

    let name_cstr = match CString::new(module_name) {
        Ok(s) => s,
        Err(_) => CString::new("fang-ext").unwrap(),
    };

    let fd = libc::memfd_create(name_cstr.as_ptr(), libc::MFD_CLOEXEC);
    if fd < 0 {
        let msg = CString::new(format!(
            "memfd_create failed for extension: {}",
            archive_path
        ))
        .unwrap();
        PyErr_SetString(PyExc_ImportError, msg.as_ptr());
        return ptr::null_mut();
    }

    {
        let mut file = std::fs::File::from_raw_fd(fd);
        if file.write_all(blob).is_err() {
            let msg = CString::new(format!(
                "failed to write extension blob for: {}",
                archive_path
            ))
            .unwrap();
            PyErr_SetString(PyExc_ImportError, msg.as_ptr());
            return ptr::null_mut();
        }
        // Flush but don't drop (would close fd) — we need fd open for dlopen.
        // Use into_raw_fd to reclaim the fd without closing.
        let _ = std::os::unix::io::IntoRawFd::into_raw_fd(file);
    }

    let path_str = format!("/proc/self/fd/{fd}");
    let path_cstr = CString::new(path_str).unwrap();
    let handle = libc::dlopen(path_cstr.as_ptr(), dlopen_flags(rtld_global));
    libc::close(fd);

    if handle.is_null() {
        let dlerr = libc::dlerror();
        let detail = if dlerr.is_null() {
            "unknown error".to_owned()
        } else {
            std::ffi::CStr::from_ptr(dlerr)
                .to_string_lossy()
                .into_owned()
        };
        let msg = CString::new(format!(
            "dlopen failed for extension {}: {}",
            archive_path, detail
        ))
        .unwrap();
        PyErr_SetString(PyExc_ImportError, msg.as_ptr());
        return ptr::null_mut();
    }

    let init_sym = init_symbol_from_archive_path(archive_path);
    call_init_fn(handle, &init_sym, archive_path, spec)
}

/// Extract any vendored `.dylib` files (e.g. `numpy/.dylibs/libopenblas.dylib`) that
/// live in the same top-level package as `archive_path` so that macOS `@loader_path`
/// RPATHs baked into the extension `.so` can resolve correctly after extraction.
#[cfg(target_os = "macos")]
fn extract_sibling_dylibs(archive: &Archive, archive_path: &str, base: &std::path::Path) {
    let rel = match archive_path.strip_prefix("extensions/") {
        Some(r) => r,
        None => return,
    };
    let Some(pkg_name) = rel.split('/').next().filter(|p| !p.is_empty()) else {
        return;
    };
    let pkg_prefix = format!("extensions/{pkg_name}/");

    for path in archive.paths() {
        if !path.starts_with(&pkg_prefix) || !path.ends_with(".dylib") {
            continue;
        }
        // Skip extension modules that happen to have a .dylib extension.
        if path.rsplit('/').next().unwrap_or("").contains(".cpython-") {
            continue;
        }
        let Some(sibling_rel) = path.strip_prefix("extensions/") else {
            continue;
        };
        if fang_pack::has_traversal(sibling_rel) {
            continue;
        }
        let cache_path = base.join(sibling_rel);
        if cache_path.exists() {
            continue;
        }
        if let Some(Ok(blob)) = archive.get(path) {
            let _ = write_cache_atomic(&cache_path, &blob);
        }
    }
}

/// Write `blob` to `cache_path` atomically using a temp file in the same directory.
///
/// The temp file is created in the same `~/Library/Caches/<app>/<ts>/` directory
/// as the final destination — never in /tmp or any system temp dir — so the
/// rename(2) is guaranteed atomic on the same filesystem volume. Mode 0o600
/// restricts the file to the current user only.
#[cfg(target_os = "macos")]
fn write_cache_atomic(cache_path: &std::path::Path, blob: &[u8]) -> std::io::Result<()> {
    use std::io::Write as _;
    use std::os::unix::fs::OpenOptionsExt;

    let parent = cache_path.parent().ok_or_else(|| {
        std::io::Error::new(std::io::ErrorKind::InvalidInput, "cache path has no parent")
    })?;
    std::fs::create_dir_all(parent)?;

    // Temp file in the same directory to guarantee same-volume atomic rename.
    let temp_path = parent.join(format!(".fang-tmp-{}", std::process::id()));

    let mut f = std::fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(&temp_path)?;
    if let Err(e) = f.write_all(blob) {
        let _ = std::fs::remove_file(&temp_path);
        return Err(e);
    }
    drop(f);
    std::fs::rename(&temp_path, cache_path)
}

#[cfg(target_os = "macos")]
unsafe fn load_extension(
    blob: &[u8],
    expected_hash: [u8; 32],
    archive_path: &str,
    _module_name: &str,
    rtld_global: bool,
    cache_dir: *const PathBuf,
    spec: *mut PyObject,
    archive: &Archive,
) -> *mut PyObject {
    // archive_path is like "extensions/numpy/core/_multiarray_umath.cpython-312-darwin.so"
    // Strip the "extensions/" prefix to get the relative sub-path.
    let rel = match archive_path.strip_prefix("extensions/") {
        Some(r) => r,
        None => archive_path,
    };

    // Task 3.4: reject traversal in the relative extension path before joining onto cache_dir.
    if fang_pack::has_traversal(rel) {
        let msg = CString::new(format!(
            "path traversal detected in extension archive path: {}",
            archive_path
        ))
        .unwrap();
        PyErr_SetString(PyExc_ImportError, msg.as_ptr());
        return ptr::null_mut();
    }

    // Fallback cache base should never be /tmp — use a subdir of Library/Caches.
    let base = if cache_dir.is_null() {
        PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| "/tmp".to_string()))
            .join("Library/Caches/fang-app/unknown")
    } else {
        (*cache_dir).clone()
    };
    let cache_path = base.join(rel);

    // Cache-hit verification: if the file exists, verify its BLAKE3 hash against the
    // expected hash from the archive index before dlopen-ing it.
    if cache_path.exists() {
        match std::fs::read(&cache_path) {
            Ok(cached_bytes) => {
                let cached_hash = *blake3::hash(&cached_bytes).as_bytes();
                if cached_hash != expected_hash {
                    // Hash mismatch — delete stale/corrupt file and re-extract below.
                    let _ = std::fs::remove_file(&cache_path);
                }
                // else: hash matches, fall through to dlopen.
            }
            Err(_) => {
                // Can't read cached file; delete and re-extract.
                let _ = std::fs::remove_file(&cache_path);
            }
        }
    }

    // Cache miss (or hash mismatch deletion above): write blob atomically.
    if !cache_path.exists() {
        if let Err(e) = write_cache_atomic(&cache_path, blob) {
            let msg = CString::new(format!(
                "failed to write cached extension {}: {}",
                archive_path, e
            ))
            .unwrap();
            PyErr_SetString(PyExc_ImportError, msg.as_ptr());
            return ptr::null_mut();
        }
    }

    // Extract vendored dylibs (e.g. numpy/.dylibs/libopenblas.dylib) so that
    // @loader_path RPATHs in the extension resolve correctly after dlopen.
    extract_sibling_dylibs(archive, archive_path, &base);

    let path_str = match cache_path.to_str() {
        Some(s) => s.to_owned(),
        None => {
            let msg = CString::new(format!(
                "non-UTF8 cache path for extension: {}",
                archive_path
            ))
            .unwrap();
            PyErr_SetString(PyExc_ImportError, msg.as_ptr());
            return ptr::null_mut();
        }
    };
    let path_cstr = match CString::new(path_str) {
        Ok(s) => s,
        Err(_) => {
            let msg = CString::new(format!(
                "NUL byte in cache path for extension: {}",
                archive_path
            ))
            .unwrap();
            PyErr_SetString(PyExc_ImportError, msg.as_ptr());
            return ptr::null_mut();
        }
    };

    let handle = libc::dlopen(path_cstr.as_ptr(), dlopen_flags(rtld_global));
    if handle.is_null() {
        let dlerr = libc::dlerror();
        let detail = if dlerr.is_null() {
            "unknown error".to_owned()
        } else {
            std::ffi::CStr::from_ptr(dlerr)
                .to_string_lossy()
                .into_owned()
        };
        let msg = CString::new(format!(
            "dlopen failed for extension {}: {}",
            archive_path, detail
        ))
        .unwrap();
        PyErr_SetString(PyExc_ImportError, msg.as_ptr());
        return ptr::null_mut();
    }

    let init_sym = init_symbol_from_archive_path(archive_path);
    call_init_fn(handle, &init_sym, archive_path, spec)
}

// ---------------------------------------------------------------------------
// importlib.resources / importlib.metadata helpers
// ---------------------------------------------------------------------------

unsafe fn resource_reader_new(archive: *const Archive, package_prefix: String) -> *mut PyObject {
    let type_ptr = FANG_RESOURCE_READER_TYPE_OBJ as *mut PyTypeObject;
    let raw = PyType_GenericAlloc(type_ptr, 0);
    if raw.is_null() {
        return ptr::null_mut();
    }
    let obj = &mut *(raw as *mut ArchiveResourceReaderObject);
    obj.archive = archive;
    obj.package_prefix = Box::into_raw(Box::new(package_prefix));
    raw
}

unsafe fn distribution_new(
    archive: *const Archive,
    dist_prefix: String,
    normalized_name: String,
) -> *mut PyObject {
    let type_ptr = FANG_DISTRIBUTION_TYPE_OBJ as *mut PyTypeObject;
    let raw = PyType_GenericAlloc(type_ptr, 0);
    if raw.is_null() {
        return ptr::null_mut();
    }
    let obj = &mut *(raw as *mut ArchiveDistributionObject);
    obj.archive = archive;
    obj.dist_prefix = Box::into_raw(Box::new(dist_prefix));
    obj.normalized_name = Box::into_raw(Box::new(normalized_name));
    raw
}

fn normalize_dist_name(name: &str) -> String {
    name.replace('-', "_").to_ascii_lowercase()
}

fn dist_name_from_prefix(prefix: &str) -> Option<String> {
    let dir = prefix.rsplit('/').next()?;
    let base = dir
        .strip_suffix(".dist-info")
        .or_else(|| dir.strip_suffix(".egg-info"))?;
    let name = base.rsplit_once('-').map(|(name, _)| name).unwrap_or(base);
    Some(normalize_dist_name(name))
}

fn resource_archive_path(prefix: &str, name: &str) -> Option<String> {
    if name.is_empty() || fang_pack::has_traversal(name) {
        return None;
    }
    Some(format!("{prefix}/{name}"))
}

unsafe extern "C" fn resource_open_resource(
    slf: *mut PyObject,
    name_obj: *mut PyObject,
) -> *mut PyObject {
    let obj = &*(slf as *const ArchiveResourceReaderObject);
    let name = match pyunicode_to_string(name_obj) {
        Some(name) => name,
        None => return ptr::null_mut(),
    };
    let prefix = &*obj.package_prefix;
    let Some(path) = resource_archive_path(prefix, &name) else {
        PyErr_SetString(PyExc_FileNotFoundError, c"resource not found".as_ptr());
        return ptr::null_mut();
    };
    let blob = match (*obj.archive).get_verified(&path) {
        Some(Ok(blob)) => blob,
        _ => {
            PyErr_SetString(PyExc_FileNotFoundError, c"resource not found".as_ptr());
            return ptr::null_mut();
        }
    };

    let io = PyImport_ImportModule(c"io".as_ptr());
    if io.is_null() {
        return ptr::null_mut();
    }
    let bytes_io = PyObject_GetAttrString(io, c"BytesIO".as_ptr());
    Py_DECREF(io);
    if bytes_io.is_null() {
        return ptr::null_mut();
    }
    let bytes = PyBytes_FromStringAndSize(blob.as_ptr() as *const c_char, blob.len() as isize);
    if bytes.is_null() {
        Py_DECREF(bytes_io);
        return ptr::null_mut();
    }
    let result = PyObject_CallFunctionObjArgs(bytes_io, bytes, ptr::null_mut::<PyObject>());
    Py_DECREF(bytes_io);
    Py_DECREF(bytes);
    result
}

unsafe extern "C" fn resource_resource_path(
    _slf: *mut PyObject,
    _name_obj: *mut PyObject,
) -> *mut PyObject {
    PyErr_SetString(
        PyExc_FileNotFoundError,
        c"fang archive resources do not have filesystem paths".as_ptr(),
    );
    ptr::null_mut()
}

unsafe extern "C" fn resource_is_resource(
    slf: *mut PyObject,
    name_obj: *mut PyObject,
) -> *mut PyObject {
    let obj = &*(slf as *const ArchiveResourceReaderObject);
    let name = match pyunicode_to_string(name_obj) {
        Some(name) => name,
        None => return ptr::null_mut(),
    };
    let prefix = &*obj.package_prefix;
    let exists =
        resource_archive_path(prefix, &name).is_some_and(|path| (*obj.archive).contains(&path));
    PyBool_FromLong(if exists { 1 } else { 0 })
}

unsafe extern "C" fn resource_contents(slf: *mut PyObject, _args: *mut PyObject) -> *mut PyObject {
    let obj = &*(slf as *const ArchiveResourceReaderObject);
    let archive = &*obj.archive;
    let prefix = &*obj.package_prefix;
    let prefix = format!("{prefix}/");
    let mut children = BTreeSet::new();
    for path in archive.paths() {
        let Some(rest) = path.strip_prefix(&prefix) else {
            continue;
        };
        let Some(child) = rest.split('/').next() else {
            continue;
        };
        if !child.is_empty() {
            children.insert(child.to_owned());
        }
    }

    let list = PyList_New(children.len() as Py_ssize_t);
    if list.is_null() {
        return ptr::null_mut();
    }
    for (idx, child) in children.iter().enumerate() {
        let item = py_string(child);
        if item.is_null() {
            Py_DECREF(list);
            return ptr::null_mut();
        }
        PyList_SetItem(list, idx as Py_ssize_t, item);
    }
    list
}

unsafe extern "C" fn distribution_read_text(
    slf: *mut PyObject,
    name_obj: *mut PyObject,
) -> *mut PyObject {
    let obj = &*(slf as *const ArchiveDistributionObject);
    let name = match pyunicode_to_string(name_obj) {
        Some(name) => name,
        None => return ptr::null_mut(),
    };
    if name.is_empty() || fang_pack::has_traversal(&name) {
        return py_none();
    }
    let path = format!("{}/{}", &*obj.dist_prefix, name);
    let Some(Ok(blob)) = (*obj.archive).get_verified(&path) else {
        return py_none();
    };
    match std::str::from_utf8(&blob) {
        Ok(text) => py_string(text),
        Err(_) => py_none(),
    }
}

unsafe fn distribution_metadata_text(obj: &ArchiveDistributionObject) -> Option<String> {
    let path = format!("{}/METADATA", &*obj.dist_prefix);
    let blob = (*obj.archive).get_verified(&path)?.ok()?;
    String::from_utf8(blob).ok()
}

unsafe extern "C" fn distribution_get_metadata(
    slf: *mut PyObject,
    _closure: *mut c_void,
) -> *mut PyObject {
    let obj = &*(slf as *const ArchiveDistributionObject);
    let text = distribution_metadata_text(obj).unwrap_or_default();
    let email = PyImport_ImportModule(c"email".as_ptr());
    if email.is_null() {
        return ptr::null_mut();
    }
    let parser = PyObject_GetAttrString(email, c"message_from_string".as_ptr());
    Py_DECREF(email);
    if parser.is_null() {
        return ptr::null_mut();
    }
    let text_obj = py_string(&text);
    if text_obj.is_null() {
        Py_DECREF(parser);
        return ptr::null_mut();
    }
    let result = PyObject_CallFunctionObjArgs(parser, text_obj, ptr::null_mut::<PyObject>());
    Py_DECREF(parser);
    Py_DECREF(text_obj);
    result
}

unsafe extern "C" fn distribution_get_version(
    slf: *mut PyObject,
    _closure: *mut c_void,
) -> *mut PyObject {
    let obj = &*(slf as *const ArchiveDistributionObject);
    let Some(text) = distribution_metadata_text(obj) else {
        return py_none();
    };
    for line in text.lines() {
        if let Some(version) = line.strip_prefix("Version:") {
            return py_string(version.trim());
        }
    }
    py_none()
}

// ---------------------------------------------------------------------------
// Method implementations
// ---------------------------------------------------------------------------

/// find_spec(fullname, path, target=None) — METH_VARARGS
unsafe extern "C" fn fang_find_spec(slf: *mut PyObject, args: *mut PyObject) -> *mut PyObject {
    let obj = &*(slf as *const FangImporterObject);
    let archive = &*obj.archive;

    let mut fullname_obj: *mut PyObject = ptr::null_mut();
    let mut _path_obj: *mut PyObject = ptr::null_mut();
    let mut _target_obj: *mut PyObject = ptr::null_mut();
    if PyArg_ParseTuple(
        args,
        c"O|OO".as_ptr(),
        &mut fullname_obj,
        &mut _path_obj,
        &mut _target_obj,
    ) == 0
    {
        return ptr::null_mut();
    }

    let fullname = match pyunicode_to_string(fullname_obj) {
        Some(s) => s,
        None => {
            if PyErr_Occurred().is_null() {
                PyErr_SetString(PyExc_ValueError, c"fullname must be a str".as_ptr());
            }
            return ptr::null_mut();
        }
    };

    // Check extension index first (O(1) lookup).
    if !obj.ext_index.is_null() {
        let ext_index = &*obj.ext_index;
        if let Some(archive_path) = ext_index.get(&fullname) {
            let origin = format!("fang://{}", archive_path);
            return build_module_spec(&fullname, slf, &origin, false);
        }
    }

    // Fall through to archive app/site-packages candidate search first.
    let candidates = module_name_to_prefixed_paths(&fullname, &["app", "site-packages"]);
    let mut matched: Option<String> = None;
    let mut is_package = false;
    for candidate in &candidates {
        if archive.contains(candidate) {
            is_package = candidate.ends_with("/__init__.pyc");
            matched = Some(candidate.clone());
            break;
        }
    }

    if let Some(asset_path) = matched {
        let origin = format!("fang://{}", asset_path);
        return build_module_spec(&fullname, slf, &origin, is_package);
    }

    if !obj.stdlib_archive.is_null() {
        let stdlib_archive = &*obj.stdlib_archive;
        if let Some((asset_path, is_package)) = runtime_stdlib_match(stdlib_archive, &fullname) {
            let origin = format!("fang://runtime-stdlib/{}", asset_path);
            return build_module_spec(&fullname, slf, &origin, is_package);
        }
    }

    // Compatibility fallback for older archives that still carry stdlib entries.
    let candidates = module_name_to_prefixed_paths(&fullname, &["stdlib"]);
    for candidate in &candidates {
        if archive.contains(candidate) {
            is_package = candidate.ends_with("/__init__.pyc");
            matched = Some(candidate.clone());
            break;
        }
    }

    match matched {
        None => {
            if let Some(origin) = namespace_origin_for_module(archive, &fullname) {
                build_namespace_spec(&fullname, &origin)
            } else {
                py_none()
            }
        }
        Some(asset_path) => {
            let origin = format!("fang://{}", asset_path);
            build_module_spec(&fullname, slf, &origin, is_package)
        }
    }
}

fn runtime_stdlib_match(archive: &Archive, fullname: &str) -> Option<(String, bool)> {
    let path = fullname.replace('.', "/");
    for candidate in [
        format!("stdlib/{path}.py"),
        format!("stdlib/{path}/__init__.py"),
    ] {
        if archive.contains(&candidate) {
            let is_package = candidate.ends_with("/__init__.py");
            return Some((candidate, is_package));
        }
    }
    None
}

fn namespace_origin_for_module(archive: &Archive, fullname: &str) -> Option<String> {
    let module_path = fullname.replace('.', "/");
    for prefix in ["app", "site-packages", "stdlib"] {
        let archive_prefix = format!("{prefix}/{module_path}/");
        if archive
            .paths()
            .any(|path| path.starts_with(&archive_prefix))
        {
            return Some(format!("fang://{prefix}/{module_path}"));
        }
    }
    None
}

unsafe fn build_namespace_spec(fullname: &str, origin: &str) -> *mut PyObject {
    let bootstrap = PyImport_ImportModule(c"_frozen_importlib".as_ptr());
    if bootstrap.is_null() {
        return ptr::null_mut();
    }
    let module_spec_cls = PyObject_GetAttrString(bootstrap, c"ModuleSpec".as_ptr());
    Py_DECREF(bootstrap);
    if module_spec_cls.is_null() {
        return ptr::null_mut();
    }

    let fullname_py =
        PyUnicode_FromStringAndSize(fullname.as_ptr() as *const c_char, fullname.len() as isize);
    if fullname_py.is_null() {
        Py_DECREF(module_spec_cls);
        return ptr::null_mut();
    }

    let origin_py =
        PyUnicode_FromStringAndSize(origin.as_ptr() as *const c_char, origin.len() as isize);
    if origin_py.is_null() {
        Py_DECREF(module_spec_cls);
        Py_DECREF(fullname_py);
        return ptr::null_mut();
    }

    let kwargs = PyDict_New();
    if kwargs.is_null() {
        Py_DECREF(module_spec_cls);
        Py_DECREF(fullname_py);
        Py_DECREF(origin_py);
        return ptr::null_mut();
    }
    if PyDict_SetItemString(kwargs, c"origin".as_ptr(), origin_py) < 0 {
        Py_DECREF(module_spec_cls);
        Py_DECREF(fullname_py);
        Py_DECREF(origin_py);
        Py_DECREF(kwargs);
        return ptr::null_mut();
    }

    let pos_args = PyTuple_Pack(2, fullname_py, Py_None());
    Py_DECREF(fullname_py);
    if pos_args.is_null() {
        Py_DECREF(module_spec_cls);
        Py_DECREF(origin_py);
        Py_DECREF(kwargs);
        return ptr::null_mut();
    }

    let spec = PyObject_Call(module_spec_cls, pos_args, kwargs);
    Py_DECREF(module_spec_cls);
    Py_DECREF(pos_args);
    Py_DECREF(kwargs);
    if spec.is_null() {
        Py_DECREF(origin_py);
        return ptr::null_mut();
    }

    let locations = PyList_New(1);
    if locations.is_null() {
        Py_DECREF(origin_py);
        Py_DECREF(spec);
        return ptr::null_mut();
    }
    PyList_SetItem(locations, 0, origin_py);
    let r = PyObject_SetAttrString(spec, c"submodule_search_locations".as_ptr(), locations);
    Py_DECREF(locations);
    if r < 0 {
        Py_DECREF(spec);
        return ptr::null_mut();
    }

    if PyObject_SetAttrString(spec, c"has_location".as_ptr(), Py_False()) < 0 {
        Py_DECREF(spec);
        return ptr::null_mut();
    }

    spec
}

unsafe fn build_module_spec(
    fullname: &str,
    loader: *mut PyObject,
    origin: &str,
    is_package: bool,
) -> *mut PyObject {
    // Use _frozen_importlib.ModuleSpec directly to avoid importing importlib.util
    // from within find_spec — that would cause infinite recursion because the
    // import system calls find_spec before sys.modules is populated.
    // _frozen_importlib is a builtin frozen module always present in sys.modules.
    let bootstrap = PyImport_ImportModule(c"_frozen_importlib".as_ptr());
    if bootstrap.is_null() {
        return ptr::null_mut();
    }
    let module_spec_cls = PyObject_GetAttrString(bootstrap, c"ModuleSpec".as_ptr());
    Py_DECREF(bootstrap);
    if module_spec_cls.is_null() {
        return ptr::null_mut();
    }

    let fullname_py =
        PyUnicode_FromStringAndSize(fullname.as_ptr() as *const c_char, fullname.len() as isize);
    if fullname_py.is_null() {
        Py_DECREF(module_spec_cls);
        return ptr::null_mut();
    }

    let origin_py =
        PyUnicode_FromStringAndSize(origin.as_ptr() as *const c_char, origin.len() as isize);
    if origin_py.is_null() {
        Py_DECREF(module_spec_cls);
        Py_DECREF(fullname_py);
        return ptr::null_mut();
    }

    // ModuleSpec(name, loader, origin=origin)
    let kwargs = PyDict_New();
    if kwargs.is_null() {
        Py_DECREF(module_spec_cls);
        Py_DECREF(fullname_py);
        Py_DECREF(origin_py);
        return ptr::null_mut();
    }
    if PyDict_SetItemString(kwargs, c"origin".as_ptr(), origin_py) < 0 {
        Py_DECREF(module_spec_cls);
        Py_DECREF(fullname_py);
        Py_DECREF(origin_py);
        Py_DECREF(kwargs);
        return ptr::null_mut();
    }
    Py_DECREF(origin_py);

    let pos_args = PyTuple_Pack(2, fullname_py, loader);
    Py_DECREF(fullname_py);
    if pos_args.is_null() {
        Py_DECREF(module_spec_cls);
        Py_DECREF(kwargs);
        return ptr::null_mut();
    }

    let spec = PyObject_Call(module_spec_cls, pos_args, kwargs);
    Py_DECREF(module_spec_cls);
    Py_DECREF(pos_args);
    Py_DECREF(kwargs);
    if spec.is_null() {
        return ptr::null_mut();
    }

    if PyObject_SetAttrString(spec, c"has_location".as_ptr(), Py_True()) < 0 {
        Py_DECREF(spec);
        return ptr::null_mut();
    }

    if is_package {
        let empty_list = PyList_New(0);
        if empty_list.is_null() {
            Py_DECREF(spec);
            return ptr::null_mut();
        }
        let r = PyObject_SetAttrString(spec, c"submodule_search_locations".as_ptr(), empty_list);
        Py_DECREF(empty_list);
        if r < 0 {
            Py_DECREF(spec);
            return ptr::null_mut();
        }
    }

    spec
}

/// get_code(fullname) — METH_O; returns the code object for a module.
/// Required by runpy.run_module which calls loader.get_code(mod_name).
unsafe extern "C" fn fang_get_code(
    slf: *mut PyObject,
    fullname_obj: *mut PyObject,
) -> *mut PyObject {
    let obj = &*(slf as *const FangImporterObject);
    let archive = &*obj.archive;

    let fullname = match pyunicode_to_string(fullname_obj) {
        Some(s) => s,
        None => {
            if PyErr_Occurred().is_null() {
                PyErr_SetString(PyExc_ValueError, c"fullname must be a str".as_ptr());
            }
            return ptr::null_mut();
        }
    };

    let candidates = module_name_to_paths(&fullname);
    let mut pyc_blob: Option<Vec<u8>> = None;
    for candidate in &candidates {
        match archive.get_verified(candidate) {
            Some(Ok(blob)) => {
                pyc_blob = Some(blob);
                break;
            }
            Some(Err(e)) => {
                let msg =
                    CString::new(format!("integrity check failed for {candidate}: {e}")).unwrap();
                PyErr_SetString(PyExc_ImportError, msg.as_ptr());
                return ptr::null_mut();
            }
            None => continue,
        }
    }

    let blob = match pyc_blob {
        Some(b) => b,
        None => {
            Py_IncRef(Py_None());
            return Py_None();
        }
    };

    if blob.len() < 17 {
        Py_IncRef(Py_None());
        return Py_None();
    }

    PyMarshal_ReadObjectFromString(
        blob[16..].as_ptr() as *const c_char,
        (blob.len() - 16) as isize,
    )
}

/// get_resource_reader(fullname) — returns a ResourceReader for packages.
unsafe extern "C" fn fang_get_resource_reader(
    slf: *mut PyObject,
    fullname_obj: *mut PyObject,
) -> *mut PyObject {
    let obj = &*(slf as *const FangImporterObject);
    let archive = &*obj.archive;
    let fullname = match pyunicode_to_string(fullname_obj) {
        Some(s) => s,
        None => return ptr::null_mut(),
    };
    let module_path = fullname.replace('.', "/");
    for prefix in ["app", "site-packages", "stdlib"] {
        let init_path = format!("{prefix}/{module_path}/__init__.pyc");
        if archive.contains(&init_path) {
            return resource_reader_new(obj.archive, format!("{prefix}/{module_path}"));
        }
    }
    py_none()
}

/// find_distributions(context=None) — importlib.metadata distribution finder hook.
unsafe extern "C" fn fang_find_distributions(
    slf: *mut PyObject,
    args: *mut PyObject,
    kwargs: *mut PyObject,
) -> *mut PyObject {
    let obj = &*(slf as *const FangImporterObject);
    let archive = &*obj.archive;

    let mut context: *mut PyObject = ptr::null_mut();
    if PyTuple_Size(args) > 0 {
        context = PyTuple_GetItem(args, 0);
    } else if !kwargs.is_null() {
        context = PyDict_GetItemString(kwargs, c"context".as_ptr());
    }

    let context_name = if !context.is_null() && context != Py_None() {
        let name_obj = PyObject_GetAttrString(context, c"name".as_ptr());
        if name_obj.is_null() {
            PyErr_Clear();
            None
        } else if name_obj == Py_None() {
            Py_DECREF(name_obj);
            None
        } else {
            let name = pyunicode_to_string(name_obj).map(|name| normalize_dist_name(&name));
            Py_DECREF(name_obj);
            name
        }
    } else {
        None
    };

    let mut prefixes = BTreeSet::new();
    for path in archive.paths() {
        let Some(rest) = path.strip_prefix("site-packages/") else {
            continue;
        };
        let Some(first) = rest.split('/').next() else {
            continue;
        };
        if first.ends_with(".dist-info") || first.ends_with(".egg-info") {
            prefixes.insert(format!("site-packages/{first}"));
        }
    }

    let list = PyList_New(0);
    if list.is_null() {
        return ptr::null_mut();
    }

    for prefix in prefixes {
        let Some(normalized_name) = dist_name_from_prefix(&prefix) else {
            continue;
        };
        if context_name
            .as_ref()
            .is_some_and(|wanted| wanted != &normalized_name)
        {
            continue;
        }
        let dist = distribution_new(obj.archive, prefix, normalized_name);
        if dist.is_null() {
            Py_DECREF(list);
            return ptr::null_mut();
        }
        if PyList_Append(list, dist) < 0 {
            Py_DECREF(dist);
            Py_DECREF(list);
            return ptr::null_mut();
        }
        Py_DECREF(dist);
    }

    list
}

/// create_module(spec) — METH_O
///
/// For extension modules (origin begins with "fang://extensions/"), performs the
/// platform-specific dlopen dance and returns the populated module object.
/// For pure Python modules, returns None (CPython creates a default module).
unsafe extern "C" fn fang_create_module(slf: *mut PyObject, spec: *mut PyObject) -> *mut PyObject {
    let obj = &*(slf as *const FangImporterObject);

    // Read spec.origin
    let origin_py = PyObject_GetAttrString(spec, c"origin".as_ptr());
    if origin_py.is_null() {
        return ptr::null_mut();
    }
    let origin = match pyunicode_to_string(origin_py) {
        Some(s) => s,
        None => {
            Py_DECREF(origin_py);
            if PyErr_Occurred().is_null() {
                PyErr_SetString(PyExc_ImportError, c"spec.origin is not a str".as_ptr());
            }
            return ptr::null_mut();
        }
    };
    Py_DECREF(origin_py);

    // Pure Python module — delegate to CPython default.
    if !origin.starts_with("fang://extensions/") {
        Py_IncRef(Py_None());
        return Py_None();
    }

    // Extension module — perform dlopen.
    let archive_path = &origin["fang://".len()..]; // "extensions/numpy/core/..."
    let module_name = match get_module_name_from_spec(spec) {
        Some(n) => n,
        None => {
            if PyErr_Occurred().is_null() {
                PyErr_SetString(PyExc_ImportError, c"could not read spec.name".as_ptr());
            }
            return ptr::null_mut();
        }
    };

    let blob = match (*obj.archive).get_verified(archive_path) {
        Some(Ok(b)) => b,
        Some(Err(e)) => {
            let msg =
                CString::new(format!("failed to load extension {}: {}", archive_path, e)).unwrap();
            PyErr_SetString(PyExc_ImportError, msg.as_ptr());
            return ptr::null_mut();
        }
        None => {
            let msg =
                CString::new(format!("extension not found in archive: {}", archive_path)).unwrap();
            PyErr_SetString(PyExc_ImportError, msg.as_ptr());
            return ptr::null_mut();
        }
    };

    let expected_hash = *blake3::hash(&blob).as_bytes();
    load_extension(
        &blob,
        expected_hash,
        archive_path,
        &module_name,
        obj.rtld_global,
        obj.cache_dir,
        spec,
        &*obj.archive,
    )
}

unsafe fn get_module_name_from_spec(spec: *mut PyObject) -> Option<String> {
    let name_py = PyObject_GetAttrString(spec, c"name".as_ptr());
    if name_py.is_null() {
        return None;
    }
    let name = pyunicode_to_string(name_py);
    Py_DECREF(name_py);
    name
}

/// exec_module(module) — METH_O
///
/// For extension modules, runs Py_mod_exec slots (multi-phase init) or no-ops
/// (single-phase init — module already fully initialized by PyInit_ in create_module).
/// For pure Python modules, decompresses .pyc, executes, and sets __file__.
unsafe extern "C" fn fang_exec_module(slf: *mut PyObject, module: *mut PyObject) -> *mut PyObject {
    let obj = &*(slf as *const FangImporterObject);
    let archive = &*obj.archive;

    // Get module.__spec__.origin (copy string before DECREF)
    let spec = PyObject_GetAttrString(module, c"__spec__".as_ptr());
    if spec.is_null() {
        return ptr::null_mut();
    }
    let origin_py = PyObject_GetAttrString(spec, c"origin".as_ptr());
    Py_DECREF(spec);
    if origin_py.is_null() {
        return ptr::null_mut();
    }
    let origin = match pyunicode_to_string(origin_py) {
        Some(s) => s,
        None => {
            Py_DECREF(origin_py);
            if PyErr_Occurred().is_null() {
                PyErr_SetString(PyExc_ImportError, c"spec.origin is not a str".as_ptr());
            }
            return ptr::null_mut();
        }
    };
    Py_DECREF(origin_py);

    // Extension modules: single-phase init is already done in create_module.
    // Multi-phase init (PEP 451) requires exec_module to run the Py_mod_exec slots.
    if origin.starts_with("fang://extensions/") {
        let def = PyModule_GetDef(module);
        if !def.is_null() {
            let r = PyModule_ExecDef(module, def);
            if r < 0 {
                return ptr::null_mut();
            }
        }
        Py_IncRef(Py_None());
        return Py_None();
    }

    if let Some(asset_path) = origin.strip_prefix("fang://runtime-stdlib/") {
        if obj.stdlib_archive.is_null() {
            let msg = CString::new(format!(
                "runtime stdlib archive unavailable: {}",
                asset_path
            ))
            .unwrap();
            PyErr_SetString(PyExc_ImportError, msg.as_ptr());
            return ptr::null_mut();
        }
        let stdlib_archive = &*obj.stdlib_archive;
        let blob = match stdlib_archive.get_verified(asset_path) {
            Some(Ok(b)) => b,
            Some(Err(e)) => {
                let msg = CString::new(format!(
                    "failed to load runtime stdlib {}: {}",
                    asset_path, e
                ))
                .unwrap();
                PyErr_SetString(PyExc_ImportError, msg.as_ptr());
                return ptr::null_mut();
            }
            None => {
                let msg = CString::new(format!("runtime stdlib asset not found: {}", asset_path))
                    .unwrap();
                PyErr_SetString(PyExc_ImportError, msg.as_ptr());
                return ptr::null_mut();
            }
        };

        let source = match CString::new(blob) {
            Ok(source) => source,
            Err(_) => {
                let msg = CString::new(format!(
                    "runtime stdlib source contains NUL: {}",
                    asset_path
                ))
                .unwrap();
                PyErr_SetString(PyExc_ImportError, msg.as_ptr());
                return ptr::null_mut();
            }
        };
        let filename = CString::new(origin.clone()).unwrap();
        let code = Py_CompileString(source.as_ptr(), filename.as_ptr(), Py_file_input);
        if code.is_null() {
            return ptr::null_mut();
        }
        return execute_code_object(module, code, &origin);
    }

    let asset_path = match origin.strip_prefix("fang://") {
        Some(p) => p.to_owned(),
        None => {
            let msg = CString::new(format!("unexpected origin: {}", origin)).unwrap();
            PyErr_SetString(PyExc_ImportError, msg.as_ptr());
            return ptr::null_mut();
        }
    };

    let blob = match archive.get_verified(&asset_path) {
        Some(Ok(b)) => b,
        Some(Err(e)) => {
            let msg = CString::new(format!("failed to load {}: {}", asset_path, e)).unwrap();
            PyErr_SetString(PyExc_ImportError, msg.as_ptr());
            return ptr::null_mut();
        }
        None => {
            let msg = CString::new(format!("asset not found: {}", asset_path)).unwrap();
            PyErr_SetString(PyExc_ImportError, msg.as_ptr());
            return ptr::null_mut();
        }
    };

    if blob.len() < 17 {
        let msg = CString::new(format!(
            "pyc blob too short ({} bytes) in {}",
            blob.len(),
            asset_path
        ))
        .unwrap();
        PyErr_SetString(PyExc_ImportError, msg.as_ptr());
        return ptr::null_mut();
    }

    // Skip 16-byte .pyc header, unmarshal code object
    let code_bytes = &blob[16..];
    let code = PyMarshal_ReadObjectFromString(
        code_bytes.as_ptr() as *const c_char,
        code_bytes.len() as isize,
    );
    if code.is_null() {
        return ptr::null_mut();
    }

    execute_code_object(module, code, &origin)
}

unsafe fn execute_code_object(
    module: *mut PyObject,
    code: *mut PyObject,
    origin: &str,
) -> *mut PyObject {
    // module.__dict__ is a borrowed ref — do not DECREF
    let module_dict = PyModule_GetDict(module);
    if module_dict.is_null() {
        Py_DECREF(code);
        return ptr::null_mut();
    }

    // PyEval_EvalCode (3.13+) no longer injects __builtins__ automatically.
    // Without it, any module-level code that touches __builtins__ (directly or
    // via eval/exec) raises KeyError: '__builtins__'.
    if PyDict_GetItemString(module_dict, c"__builtins__".as_ptr()).is_null() {
        let builtins = PyImport_ImportModule(c"builtins".as_ptr());
        if builtins.is_null() {
            Py_DECREF(code);
            return ptr::null_mut();
        }
        let r = PyDict_SetItemString(module_dict, c"__builtins__".as_ptr(), builtins);
        Py_DECREF(builtins);
        if r < 0 {
            Py_DECREF(code);
            return ptr::null_mut();
        }
    }

    let result = PyEval_EvalCode(code, module_dict, module_dict);
    Py_DECREF(code);
    if result.is_null() {
        return ptr::null_mut();
    }
    Py_DECREF(result);

    // Set module.__file__ = origin string
    let file_py =
        PyUnicode_FromStringAndSize(origin.as_ptr() as *const c_char, origin.len() as isize);
    if file_py.is_null() {
        return ptr::null_mut();
    }
    let r = PyObject_SetAttrString(module, c"__file__".as_ptr(), file_py);
    Py_DECREF(file_py);
    if r < 0 {
        return ptr::null_mut();
    }

    Py_IncRef(Py_None());
    Py_None()
}

// ---------------------------------------------------------------------------
// Type initialization via PyType_FromSpec
// ---------------------------------------------------------------------------

pub unsafe fn init_type() -> c_int {
    if !FANG_IMPORTER_TYPE_OBJ.is_null() {
        return 0;
    }

    if init_resource_reader_type() != 0 || init_distribution_type() != 0 {
        return -1;
    }

    let methods: Box<[PyMethodDef; 7]> = Box::new([
        PyMethodDef {
            ml_name: c"find_spec".as_ptr(),
            ml_meth: PyMethodDefPointer {
                PyCFunction: fang_find_spec,
            },
            ml_flags: METH_VARARGS,
            ml_doc: ptr::null(),
        },
        PyMethodDef {
            ml_name: c"get_code".as_ptr(),
            ml_meth: PyMethodDefPointer {
                PyCFunction: fang_get_code,
            },
            ml_flags: METH_O,
            ml_doc: ptr::null(),
        },
        PyMethodDef {
            ml_name: c"create_module".as_ptr(),
            ml_meth: PyMethodDefPointer {
                PyCFunction: fang_create_module,
            },
            ml_flags: METH_O,
            ml_doc: ptr::null(),
        },
        PyMethodDef {
            ml_name: c"exec_module".as_ptr(),
            ml_meth: PyMethodDefPointer {
                PyCFunction: fang_exec_module,
            },
            ml_flags: METH_O,
            ml_doc: ptr::null(),
        },
        PyMethodDef {
            ml_name: c"get_resource_reader".as_ptr(),
            ml_meth: PyMethodDefPointer {
                PyCFunction: fang_get_resource_reader,
            },
            ml_flags: METH_O,
            ml_doc: ptr::null(),
        },
        PyMethodDef {
            ml_name: c"find_distributions".as_ptr(),
            ml_meth: PyMethodDefPointer {
                PyCFunctionWithKeywords: fang_find_distributions,
            },
            ml_flags: METH_VARARGS | METH_KEYWORDS,
            ml_doc: ptr::null(),
        },
        PyMethodDef::zeroed(),
    ]);
    let methods_ptr = Box::leak(methods).as_mut_ptr();

    let mut slots: [PyType_Slot; 3] = [
        PyType_Slot {
            slot: Py_tp_methods,
            pfunc: methods_ptr as *mut c_void,
        },
        PyType_Slot {
            slot: Py_tp_new,
            pfunc: PyType_GenericNew as *mut c_void,
        },
        PyType_Slot {
            slot: 0,
            pfunc: ptr::null_mut(),
        },
    ];

    let mut spec = PyType_Spec {
        name: c"fang_importer.FangImporter".as_ptr(),
        basicsize: std::mem::size_of::<FangImporterObject>() as c_int,
        itemsize: 0,
        flags: Py_TPFLAGS_DEFAULT as c_uint,
        slots: slots.as_mut_ptr(),
    };

    let type_obj = PyType_FromSpec(&mut spec);
    if type_obj.is_null() {
        return -1;
    }

    FANG_IMPORTER_TYPE_OBJ = type_obj;
    0
}

unsafe fn init_resource_reader_type() -> c_int {
    if !FANG_RESOURCE_READER_TYPE_OBJ.is_null() {
        return 0;
    }

    let methods: Box<[PyMethodDef; 5]> = Box::new([
        PyMethodDef {
            ml_name: c"open_resource".as_ptr(),
            ml_meth: PyMethodDefPointer {
                PyCFunction: resource_open_resource,
            },
            ml_flags: METH_O,
            ml_doc: ptr::null(),
        },
        PyMethodDef {
            ml_name: c"resource_path".as_ptr(),
            ml_meth: PyMethodDefPointer {
                PyCFunction: resource_resource_path,
            },
            ml_flags: METH_O,
            ml_doc: ptr::null(),
        },
        PyMethodDef {
            ml_name: c"is_resource".as_ptr(),
            ml_meth: PyMethodDefPointer {
                PyCFunction: resource_is_resource,
            },
            ml_flags: METH_O,
            ml_doc: ptr::null(),
        },
        PyMethodDef {
            ml_name: c"contents".as_ptr(),
            ml_meth: PyMethodDefPointer {
                PyCFunction: resource_contents,
            },
            ml_flags: METH_NOARGS,
            ml_doc: ptr::null(),
        },
        PyMethodDef::zeroed(),
    ]);
    let methods_ptr = Box::leak(methods).as_mut_ptr();

    let mut slots: [PyType_Slot; 3] = [
        PyType_Slot {
            slot: Py_tp_methods,
            pfunc: methods_ptr as *mut c_void,
        },
        PyType_Slot {
            slot: Py_tp_new,
            pfunc: PyType_GenericNew as *mut c_void,
        },
        PyType_Slot {
            slot: 0,
            pfunc: ptr::null_mut(),
        },
    ];

    let mut spec = PyType_Spec {
        name: c"fang_importer.ArchiveResourceReader".as_ptr(),
        basicsize: std::mem::size_of::<ArchiveResourceReaderObject>() as c_int,
        itemsize: 0,
        flags: Py_TPFLAGS_DEFAULT as c_uint,
        slots: slots.as_mut_ptr(),
    };

    let type_obj = PyType_FromSpec(&mut spec);
    if type_obj.is_null() {
        return -1;
    }
    FANG_RESOURCE_READER_TYPE_OBJ = type_obj;
    0
}

unsafe fn init_distribution_type() -> c_int {
    if !FANG_DISTRIBUTION_TYPE_OBJ.is_null() {
        return 0;
    }

    let methods: Box<[PyMethodDef; 2]> = Box::new([
        PyMethodDef {
            ml_name: c"read_text".as_ptr(),
            ml_meth: PyMethodDefPointer {
                PyCFunction: distribution_read_text,
            },
            ml_flags: METH_O,
            ml_doc: ptr::null(),
        },
        PyMethodDef::zeroed(),
    ]);
    let methods_ptr = Box::leak(methods).as_mut_ptr();

    let getset: Box<[PyGetSetDef; 3]> = Box::new([
        PyGetSetDef {
            name: c"metadata".as_ptr(),
            get: Some(distribution_get_metadata),
            set: None,
            doc: ptr::null(),
            closure: ptr::null_mut(),
        },
        PyGetSetDef {
            name: c"version".as_ptr(),
            get: Some(distribution_get_version),
            set: None,
            doc: ptr::null(),
            closure: ptr::null_mut(),
        },
        PyGetSetDef::default(),
    ]);
    let getset_ptr = Box::leak(getset).as_mut_ptr();

    let mut slots: [PyType_Slot; 4] = [
        PyType_Slot {
            slot: Py_tp_methods,
            pfunc: methods_ptr as *mut c_void,
        },
        PyType_Slot {
            slot: Py_tp_getset,
            pfunc: getset_ptr as *mut c_void,
        },
        PyType_Slot {
            slot: Py_tp_new,
            pfunc: PyType_GenericNew as *mut c_void,
        },
        PyType_Slot {
            slot: 0,
            pfunc: ptr::null_mut(),
        },
    ];

    let mut spec = PyType_Spec {
        name: c"fang_importer.ArchiveDistribution".as_ptr(),
        basicsize: std::mem::size_of::<ArchiveDistributionObject>() as c_int,
        itemsize: 0,
        flags: Py_TPFLAGS_DEFAULT as c_uint,
        slots: slots.as_mut_ptr(),
    };

    let type_obj = PyType_FromSpec(&mut spec);
    if type_obj.is_null() {
        return -1;
    }
    FANG_DISTRIBUTION_TYPE_OBJ = type_obj;
    0
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/// Install a FangImporter backed by `archive` at the front of sys.meta_path.
///
/// - `archive`: process-lifetime pointer to the loaded archive
/// - `ext_index`: process-lifetime pointer to the extension module name → archive path map
/// - `rtld_global`: whether to dlopen with RTLD_GLOBAL (true) or RTLD_LOCAL (false)
/// - `cache_dir`: on macOS, the base cache directory for extracted dylibs; null on Linux
///
/// Must be called after Py_Initialize.
pub unsafe fn install_fang_importer(
    archive: *const Archive,
    stdlib_archive: *const Archive,
    ext_index: *const HashMap<String, String>,
    rtld_global: bool,
    cache_dir: *const PathBuf,
) -> Result<(), String> {
    if init_type() != 0 {
        return Err("PyType_Ready failed for FangImporter".into());
    }

    let type_ptr = FANG_IMPORTER_TYPE_OBJ as *mut PyTypeObject;
    let raw = PyType_GenericAlloc(type_ptr, 0);
    if raw.is_null() {
        return Err("allocation failed for FangImporterObject".into());
    }

    let obj = &mut *(raw as *mut FangImporterObject);
    obj.archive = archive;
    obj.stdlib_archive = stdlib_archive;
    obj.ext_index = ext_index;
    obj.rtld_global = rtld_global;
    obj.cache_dir = cache_dir;

    let sys = PyImport_ImportModule(c"sys".as_ptr());
    if sys.is_null() {
        Py_DECREF(raw);
        return Err("failed to import sys".into());
    }
    let meta_path = PyObject_GetAttrString(sys, c"meta_path".as_ptr());
    Py_DECREF(sys);
    if meta_path.is_null() {
        Py_DECREF(raw);
        return Err("failed to get sys.meta_path".into());
    }

    let r = PyList_Insert(meta_path, 0, raw);
    Py_DECREF(meta_path);
    Py_DECREF(raw);

    if r < 0 {
        Err("PyList_Insert on sys.meta_path failed".into())
    } else {
        Ok(())
    }
}

#[cfg(test)]
#[cfg(target_os = "macos")]
mod cache_tests {
    use super::write_cache_atomic;

    #[test]
    fn atomic_write_creates_file_with_correct_content() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("sub").join("ext.dylib");
        let blob = b"fake dylib content";
        write_cache_atomic(&target, blob).unwrap();
        assert!(target.exists());
        assert_eq!(std::fs::read(&target).unwrap(), blob);
    }

    #[test]
    fn atomic_write_no_temp_file_left_on_success() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("ext.dylib");
        write_cache_atomic(&target, b"blob").unwrap();
        // The temp file named .fang-tmp-<pid> should not remain after rename
        let pid = std::process::id();
        let temp = dir.path().join(format!(".fang-tmp-{pid}"));
        assert!(!temp.exists(), "stale temp file left behind");
    }

    #[test]
    fn corrupt_cache_file_detected_by_hash() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("ext.dylib");
        let blob = b"real dylib content";

        // Write correct file first
        write_cache_atomic(&target, blob).unwrap();

        // Corrupt it
        std::fs::write(&target, b"corrupted!").unwrap();

        // Simulate what load_extension does: read, hash, compare
        let expected_hash = *blake3::hash(blob).as_bytes();
        let cached = std::fs::read(&target).unwrap();
        let cached_hash = *blake3::hash(&cached).as_bytes();
        assert_ne!(
            cached_hash, expected_hash,
            "test setup: should be different"
        );

        // On mismatch, delete and re-extract (here we just verify deletion works)
        std::fs::remove_file(&target).unwrap();
        write_cache_atomic(&target, blob).unwrap();
        let restored = std::fs::read(&target).unwrap();
        assert_eq!(restored, blob);
        assert_eq!(*blake3::hash(&restored).as_bytes(), expected_hash);
    }
}
