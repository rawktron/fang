mod importer;
mod paths;

pub use importer::install_fang_importer;

#[cfg(feature = "extension-module")]
mod ext;

#[cfg(feature = "extension-module")]
#[allow(non_snake_case)]
#[no_mangle]
pub unsafe extern "C" fn PyInit_fang_importer() -> *mut pyo3_ffi::PyObject {
    ext::init_module()
}
