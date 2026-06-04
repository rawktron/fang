use crate::importer::install_fang_importer;
use fang_pack::Archive;
use pyo3_ffi::*;
use std::ffi::CString;

use std::ptr;

/// install_from_bytes(data: bytes) -> None
/// Parses the archive from bytes, leaks it, and installs FangImporter.
unsafe extern "C" fn py_install_from_bytes(
    _module: *mut PyObject,
    arg: *mut PyObject,
) -> *mut PyObject {
    // arg is a bytes object (METH_O) — use PyBytes API to avoid PY_SSIZE_T_CLEAN issues
    if PyBytes_Check(arg) == 0 {
        PyErr_SetString(PyExc_TypeError, c"expected bytes".as_ptr());
        return ptr::null_mut();
    }
    let data_ptr = PyBytes_AsString(arg) as *const u8;
    let data_len = PyBytes_Size(arg) as usize;
    let data = std::slice::from_raw_parts(data_ptr, data_len);

    let archive = match Archive::from_bytes(data) {
        Ok(a) => a,
        Err(e) => {
            let msg = CString::new(format!("invalid fang archive: {}", e)).unwrap();
            PyErr_SetString(PyExc_ValueError, msg.as_ptr());
            return ptr::null_mut();
        }
    };

    // Read extension index from meta (empty is fine for the test extension module).
    let ext_index = match archive.meta() {
        Ok(meta) => Box::into_raw(Box::new(meta.extensions))
            as *const std::collections::HashMap<String, String>,
        Err(_) => Box::into_raw(Box::new(std::collections::HashMap::new()))
            as *const std::collections::HashMap<String, String>,
    };
    let rtld_global = true;

    let archive_ptr = Box::into_raw(Box::new(archive)) as *const Archive;
    match install_fang_importer(
        archive_ptr,
        std::ptr::null(),
        ext_index,
        rtld_global,
        std::ptr::null(),
    ) {
        Ok(()) => {
            Py_IncRef(Py_None());
            Py_None()
        }
        Err(e) => {
            let msg = CString::new(format!("install_fang_importer failed: {}", e)).unwrap();
            PyErr_SetString(PyExc_RuntimeError, msg.as_ptr());
            // Leak the archive anyway since the importer may have partially installed
            ptr::null_mut()
        }
    }
}

pub unsafe fn init_module() -> *mut PyObject {
    let methods: Box<[PyMethodDef; 2]> = Box::new([
        PyMethodDef {
            ml_name: c"install_from_bytes".as_ptr(),
            ml_meth: PyMethodDefPointer {
                PyCFunction: py_install_from_bytes,
            },
            ml_flags: METH_O,
            ml_doc: ptr::null(),
        },
        PyMethodDef::zeroed(),
    ]);
    let methods_ptr = Box::leak(methods).as_mut_ptr();

    let def = Box::leak(Box::new(PyModuleDef {
        m_base: PyModuleDef_HEAD_INIT,
        m_name: c"fang_importer".as_ptr(),
        m_doc: ptr::null(),
        m_size: -1,
        m_methods: methods_ptr,
        m_slots: ptr::null_mut(),
        m_traverse: None,
        m_clear: None,
        m_free: None,
    }));

    PyModule_Create(def as *mut _)
}
