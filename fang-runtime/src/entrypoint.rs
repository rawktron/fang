use pyo3_ffi::*;

pub unsafe fn set_sys_argv(args: &[String]) {
    let list = PyList_New(args.len() as Py_ssize_t);
    if list.is_null() {
        return;
    }
    for (i, arg) in args.iter().enumerate() {
        let s = PyUnicode_FromStringAndSize(
            arg.as_ptr() as *const std::os::raw::c_char,
            arg.len() as Py_ssize_t,
        );
        if s.is_null() {
            Py_DECREF(list);
            return;
        }
        // PyList_SetItem steals the reference
        PyList_SetItem(list, i as Py_ssize_t, s);
    }
    PySys_SetObject(c"argv".as_ptr(), list);
    Py_DECREF(list);
}

/// Runs either a callable entrypoint or `runpy.run_module(entry_point, ...)`.
/// Returns the process exit code.
pub unsafe fn run_entrypoint(entry_point: &str, entry_callable: Option<&str>) -> i32 {
    if let Some(callable) = entry_callable {
        return run_callable_entrypoint(entry_point, callable);
    }

    let runpy = PyImport_ImportModule(c"runpy".as_ptr());
    if runpy.is_null() {
        eprintln!("fang: failed to import runpy");
        PyErr_Print();
        return 1;
    }

    let run_module = PyObject_GetAttrString(runpy, c"run_module".as_ptr());
    Py_DECREF(runpy);
    if run_module.is_null() {
        eprintln!("fang: runpy has no attribute 'run_module'");
        PyErr_Print();
        return 1;
    }

    let ep_str = PyUnicode_FromStringAndSize(
        entry_point.as_ptr() as *const std::os::raw::c_char,
        entry_point.len() as Py_ssize_t,
    );
    if ep_str.is_null() {
        Py_DECREF(run_module);
        return 1;
    }

    let pos_args = PyTuple_New(1);
    if pos_args.is_null() {
        Py_DECREF(run_module);
        Py_DECREF(ep_str);
        return 1;
    }
    // PyTuple_SetItem steals the reference to ep_str
    PyTuple_SetItem(pos_args, 0, ep_str);

    let kwargs = PyDict_New();
    if kwargs.is_null() {
        Py_DECREF(run_module);
        Py_DECREF(pos_args);
        return 1;
    }

    let run_name_val = PyUnicode_FromString(c"__main__".as_ptr());
    PyDict_SetItemString(kwargs, c"run_name".as_ptr(), run_name_val);
    Py_XDECREF(run_name_val);
    PyDict_SetItemString(kwargs, c"alter_sys".as_ptr(), Py_True());

    let result = PyObject_Call(run_module, pos_args, kwargs);
    Py_DECREF(run_module);
    Py_DECREF(pos_args);
    Py_DECREF(kwargs);

    if !result.is_null() {
        Py_DECREF(result);
        return 0;
    }

    extract_exit_code()
}

unsafe fn run_callable_entrypoint(entry_point: &str, entry_callable: &str) -> i32 {
    let module_name = PyUnicode_FromStringAndSize(
        entry_point.as_ptr() as *const std::os::raw::c_char,
        entry_point.len() as Py_ssize_t,
    );
    if module_name.is_null() {
        return 1;
    }

    let module = PyImport_Import(module_name);
    Py_DECREF(module_name);
    if module.is_null() {
        PyErr_Print();
        return 1;
    }

    let callable_name = match std::ffi::CString::new(entry_callable) {
        Ok(name) => name,
        Err(_) => {
            Py_DECREF(module);
            eprintln!("fang: callable entrypoint contains an interior NUL byte");
            return 1;
        }
    };
    let callable = PyObject_GetAttrString(module, callable_name.as_ptr());
    Py_DECREF(module);
    if callable.is_null() {
        PyErr_Print();
        return 1;
    }

    let result = PyObject_CallNoArgs(callable);
    Py_DECREF(callable);
    if !result.is_null() {
        Py_DECREF(result);
        return 0;
    }

    extract_exit_code()
}

unsafe fn extract_exit_code() -> i32 {
    if PyErr_ExceptionMatches(PyExc_SystemExit) == 0 {
        PyErr_Print();
        return 1;
    }

    let pvalue = PyErr_GetRaisedException();

    let code = if pvalue.is_null() || pvalue == Py_None() {
        0
    } else {
        let code_attr = PyObject_GetAttrString(pvalue, c"code".as_ptr());
        if code_attr.is_null() || code_attr == Py_None() {
            Py_XDECREF(code_attr);
            0
        } else if PyLong_Check(code_attr) != 0 {
            let n = PyLong_AsLong(code_attr) as i32;
            Py_DECREF(code_attr);
            n
        } else {
            // Non-integer exit code — treat as 1
            Py_DECREF(code_attr);
            1
        }
    };

    Py_XDECREF(pvalue);
    code
}
