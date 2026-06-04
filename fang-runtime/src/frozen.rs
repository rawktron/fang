use std::os::raw::{c_char, c_int, c_void};

include!(concat!(env!("OUT_DIR"), "/cpython/frozen_bootstrap.rs"));

// Must match CPython 3.12's `struct _frozen` from cpython/import.h exactly.
// The struct gained `is_package` and `get_code` in 3.12 — missing fields cause
// a stride mismatch that makes CPython read garbage as function pointers.
#[repr(C)]
struct FrozenEntry {
    name: *const c_char,
    code: *const u8,
    size: c_int,
    is_package: c_int,
    get_code: Option<unsafe extern "C" fn() -> *mut c_void>,
}

unsafe impl Sync for FrozenEntry {}

extern "C" {
    static mut PyImport_FrozenModules: *const FrozenEntry;
}

static ENCODINGS_NAME: &[u8] = b"encodings\0";
static ENCODINGS_ALIASES_NAME: &[u8] = b"encodings.aliases\0";
static ENCODINGS_UTF8_NAME: &[u8] = b"encodings.utf_8\0";

/// Prepend frozen `encodings`, `encodings.aliases`, and `encodings.utf_8` to
/// `PyImport_FrozenModules` so that `Py_InitializeEx` can resolve the
/// filesystem encoding codec without touching the host filesystem.
///
/// Must be called before `Py_InitializeEx`.
pub unsafe fn install_frozen_bootstrap() {
    // In CPython 3.12, PyImport_FrozenModules defaults to NULL.
    let existing = PyImport_FrozenModules;
    let mut n = 0usize;
    if !existing.is_null() {
        while !(*existing.add(n)).name.is_null() {
            n += 1;
        }
    }

    let total = 3 + n + 1;
    let mut entries: Vec<FrozenEntry> = Vec::with_capacity(total);

    let (enc_code, _) = ENCODINGS_BYTECODE;
    let (aliases_code, _) = ENCODINGS_ALIASES_BYTECODE;
    let (utf8_code, _) = ENCODINGS_UTF8_BYTECODE;

    entries.push(FrozenEntry {
        name: ENCODINGS_NAME.as_ptr() as *const c_char,
        code: enc_code.as_ptr(),
        size: -(enc_code.len() as c_int),
        is_package: 1,
        get_code: None,
    });
    entries.push(FrozenEntry {
        name: ENCODINGS_ALIASES_NAME.as_ptr() as *const c_char,
        code: aliases_code.as_ptr(),
        size: aliases_code.len() as c_int,
        is_package: 0,
        get_code: None,
    });
    entries.push(FrozenEntry {
        name: ENCODINGS_UTF8_NAME.as_ptr() as *const c_char,
        code: utf8_code.as_ptr(),
        size: utf8_code.len() as c_int,
        is_package: 0,
        get_code: None,
    });

    if !existing.is_null() {
        for i in 0..n {
            let e = &*existing.add(i);
            entries.push(FrozenEntry {
                name: e.name,
                code: e.code,
                size: e.size,
                is_package: e.is_package,
                get_code: e.get_code,
            });
        }
    }

    entries.push(FrozenEntry {
        name: std::ptr::null(),
        code: std::ptr::null(),
        size: 0,
        is_package: 0,
        get_code: None,
    });

    let ptr = entries.as_ptr();
    std::mem::forget(entries);
    PyImport_FrozenModules = ptr;
}
