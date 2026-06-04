use std::collections::HashMap;
use std::ffi::CString;

use fang_importer::install_fang_importer;
use fang_pack::{Archive, ArchiveBuilder, Meta};
use pyo3_ffi::*;

use crate::python_init::{init_cpython, prewarm_stdlib_modules, remove_path_finder};
use crate::runtime_stdlib;

pub enum ToolCommand {
    CompileBytecode { dir: String },
}

pub struct ToolArgs {
    pub command: ToolCommand,
    pub python_version: Option<String>,
}

/// Run fang-runtime as a plain Python interpreter using the embedded runtime stdlib.
/// Used when the binary has no embedded app archive — lets uv and other tools probe
/// Python version/prefix/sysconfig without a real filesystem stdlib installation.
pub fn run_plain_interpreter(program_name: &str, argv: &[String]) -> i32 {
    unsafe { run_plain_interpreter_inner(program_name, argv) }
}

unsafe fn run_plain_interpreter_inner(program_name: &str, argv: &[String]) -> i32 {
    if let Err(e) = init_cpython(program_name) {
        eprintln!("{e}");
        return 1;
    }

    let runtime_stdlib = match runtime_stdlib::archive() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("fang: failed to load runtime stdlib archive: {e}");
            Py_Finalize();
            return 1;
        }
    };
    let runtime_stdlib_ptr = Box::into_raw(Box::new(runtime_stdlib));

    if let Err(e) = setup_tool_meta_path(runtime_stdlib_ptr) {
        eprintln!("fang: {e}");
        Py_Finalize();
        return 1;
    }

    if let Err(e) = prewarm_stdlib_modules() {
        eprintln!("{e}");
        Py_Finalize();
        return 1;
    }

    let exit_code = execute_argv(argv);
    Py_Finalize();
    exit_code
}

unsafe fn execute_argv(argv: &[String]) -> i32 {
    let mut i = 1usize; // skip argv[0] (program name)

    // Walk standard Python flags before the actual command.
    // Flags that consume the next token as their value:
    const FLAGS_WITH_VALUE: &[&str] = &["-W", "-X", "-Q"];
    // Standalone boolean flags:
    const FLAGS_STANDALONE: &[&str] = &[
        "-B", "-b", "-d", "-E", "-i", "-I", "-O", "-OO", "-P", "-q", "-R", "-s", "-S", "-u",
        "-v", "--",
    ];
    while i < argv.len() {
        let arg = argv[i].as_str();
        if FLAGS_WITH_VALUE.contains(&arg) {
            i += 2;
        } else if FLAGS_STANDALONE.contains(&arg)
            || arg.starts_with("-W")
            || arg.starts_with("-X")
        {
            i += 1;
        } else {
            break;
        }
    }

    let Some(cmd) = argv.get(i) else {
        return 0;
    };

    match cmd.as_str() {
        "-c" => {
            let Some(code) = argv.get(i + 1) else {
                eprintln!("fang: -c requires a code argument");
                return 1;
            };
            let Ok(code_c) = CString::new(code.as_str()) else {
                eprintln!("fang: -c code argument contains a NUL byte");
                return 1;
            };
            let result = PyRun_SimpleString(code_c.as_ptr());
            if result == 0 { 0 } else { 1 }
        }
        "-V" | "--version" => {
            let sys = PyImport_ImportModule(c"sys".as_ptr());
            if sys.is_null() {
                return 1;
            }
            let version = PyObject_GetAttrString(sys, c"version".as_ptr());
            Py_DECREF(sys);
            if version.is_null() {
                return 1;
            }
            let mut size: pyo3_ffi::Py_ssize_t = 0;
            let ptr = PyUnicode_AsUTF8AndSize(version, &mut size);
            if !ptr.is_null() {
                let bytes =
                    std::slice::from_raw_parts(ptr as *const u8, size as usize);
                if let Ok(s) = std::str::from_utf8(bytes) {
                    println!("Python {s}");
                }
            }
            Py_DECREF(version);
            0
        }
        _ => {
            eprintln!("fang: unsupported plain-interpreter invocation: {cmd}");
            1
        }
    }
}

/// Returns `Some(ToolArgs)` if `--fang-tool` is present in `argv`, `None` otherwise.
pub fn parse_tool_args(argv: &[String]) -> Option<ToolArgs> {
    let tool_idx = argv.iter().position(|a| a == "--fang-tool")?;
    let command_str = argv.get(tool_idx + 1)?;
    match command_str.as_str() {
        "compile-bytecode" => {
            let python_version = flag_value(argv, "--python-version");
            let dir = flag_value(argv, "--in-place")?;
            Some(ToolArgs {
                command: ToolCommand::CompileBytecode { dir },
                python_version,
            })
        }
        _ => None,
    }
}

fn flag_value(argv: &[String], flag: &str) -> Option<String> {
    let idx = argv.iter().position(|a| a == flag)?;
    argv.get(idx + 1).cloned()
}

pub fn run_tool_mode(program_name: &str, args: ToolArgs) -> i32 {
    unsafe { run_tool_mode_inner(program_name, args) }
}

unsafe fn run_tool_mode_inner(program_name: &str, args: ToolArgs) -> i32 {
    if let Err(e) = init_cpython(program_name) {
        eprintln!("fang-tool: {e}");
        return 1;
    }

    if let Some(ref version) = args.python_version {
        if let Err(e) = validate_python_series(version) {
            eprintln!("fang-tool: {e}");
            Py_Finalize();
            return 1;
        }
    }

    let runtime_stdlib = match runtime_stdlib::archive() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("fang-tool: failed to load runtime stdlib: {e}");
            Py_Finalize();
            return 1;
        }
    };
    let runtime_stdlib_ptr = Box::into_raw(Box::new(runtime_stdlib));

    if let Err(e) = setup_tool_meta_path(runtime_stdlib_ptr) {
        eprintln!("fang-tool: {e}");
        Py_Finalize();
        return 1;
    }

    if let Err(e) = prewarm_stdlib_modules() {
        eprintln!("fang-tool: {e}");
        Py_Finalize();
        return 1;
    }

    let exit_code = match args.command {
        ToolCommand::CompileBytecode { ref dir } => compile_bytecode(dir),
    };

    Py_Finalize();
    exit_code
}

unsafe fn validate_python_series(required: &str) -> Result<(), String> {
    let base = required.split_once('+').map(|(v, _)| v).unwrap_or(required);
    let mut parts = base.splitn(3, '.');
    let req_major: i32 = parts.next().and_then(|s| s.parse().ok()).unwrap_or(-1);
    let req_minor: i32 = parts.next().and_then(|s| s.parse().ok()).unwrap_or(-1);

    let sys = PyImport_ImportModule(c"sys".as_ptr());
    if sys.is_null() {
        return Err("failed to import sys".into());
    }
    let vi = PyObject_GetAttrString(sys, c"version_info".as_ptr());
    Py_DECREF(sys);
    if vi.is_null() {
        return Err("failed to get sys.version_info".into());
    }
    let linked_major = PyLong_AsLong(PySequence_GetItem(vi, 0)) as i32;
    let linked_minor = PyLong_AsLong(PySequence_GetItem(vi, 1)) as i32;
    Py_DECREF(vi);

    if req_major != -1 && (req_major != linked_major || req_minor != linked_minor) {
        return Err(format!(
            "tool mode requires Python {req_major}.{req_minor} \
             but runtime links Python {linked_major}.{linked_minor}"
        ));
    }
    Ok(())
}

unsafe fn setup_tool_meta_path(runtime_stdlib: *const Archive) -> Result<(), String> {
    let fang_prefix = PyUnicode_FromString(c"fang://".as_ptr());
    if fang_prefix.is_null() {
        return Err("failed to create fang:// prefix string".into());
    }
    PySys_SetObject(c"prefix".as_ptr(), fang_prefix);
    PySys_SetObject(c"exec_prefix".as_ptr(), fang_prefix);
    Py_DECREF(fang_prefix);

    let empty_list = PyList_New(0);
    if empty_list.is_null() {
        return Err("failed to create empty sys.path".into());
    }
    PySys_SetObject(c"path".as_ptr(), empty_list);
    Py_DECREF(empty_list);

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

    let empty_archive = empty_app_archive().map_err(|e| format!("empty archive: {e}"))?;
    let empty_archive_ptr = Box::into_raw(Box::new(empty_archive)) as *const Archive;
    let ext_index = Box::into_raw(Box::new(HashMap::<String, String>::new()));

    install_fang_importer(
        empty_archive_ptr,
        runtime_stdlib,
        ext_index,
        false,
        std::ptr::null(),
    )
    .map_err(|e| format!("install_fang_importer: {e}"))
}

fn empty_app_archive() -> fang_pack::Result<Archive> {
    let mut b = ArchiveBuilder::new();
    b.set_meta(Meta {
        python_version: "0.0.0".into(),
        entry_point: "__tool__".into(),
        entry_callable: None,
        platform: "tool".into(),
        build_timestamp: "tool".into(),
        project_name: "fang-tool".into(),
        extensions: HashMap::new(),
        native_libs: Vec::new(),
        rtld_global: false,
    });
    Archive::from_bytes(&b.build()?)
}

/// Compile all `.py` files under `dir` to adjacent `.pyc` files (legacy mode).
/// Returns 0 on success, 1 on any compile failure.
unsafe fn compile_bytecode(dir: &str) -> i32 {
    let compileall = PyImport_ImportModule(c"compileall".as_ptr());
    if compileall.is_null() {
        PyErr_Print();
        return 1;
    }

    let compile_dir_fn = PyObject_GetAttrString(compileall, c"compile_dir".as_ptr());
    Py_DECREF(compileall);
    if compile_dir_fn.is_null() {
        PyErr_Print();
        return 1;
    }

    let dir_cstr = match CString::new(dir) {
        Ok(s) => s,
        Err(_) => {
            eprintln!("fang-tool: compile-bytecode: directory path contains NUL");
            Py_DECREF(compile_dir_fn);
            return 1;
        }
    };
    let dir_py = PyUnicode_FromString(dir_cstr.as_ptr());
    if dir_py.is_null() {
        Py_DECREF(compile_dir_fn);
        return 1;
    }

    let kwargs = PyDict_New();
    if kwargs.is_null() {
        Py_DECREF(compile_dir_fn);
        Py_DECREF(dir_py);
        return 1;
    }

    // legacy=True: write .pyc adjacent to source (not in __pycache__)
    PyDict_SetItemString(kwargs, c"legacy".as_ptr(), Py_True());
    // force=True: always recompile even if .pyc is up to date
    PyDict_SetItemString(kwargs, c"force".as_ptr(), Py_True());
    // quiet=1: print compile errors but suppress "Compiling X" progress messages
    let quiet = PyLong_FromLong(1);
    PyDict_SetItemString(kwargs, c"quiet".as_ptr(), quiet);
    Py_DECREF(quiet);

    let pos_args = PyTuple_Pack(1, dir_py);
    Py_DECREF(dir_py);
    if pos_args.is_null() {
        Py_DECREF(compile_dir_fn);
        Py_DECREF(kwargs);
        return 1;
    }

    let result = PyObject_Call(compile_dir_fn, pos_args, kwargs);
    Py_DECREF(compile_dir_fn);
    Py_DECREF(pos_args);
    Py_DECREF(kwargs);

    if result.is_null() {
        PyErr_Print();
        return 1;
    }

    let ok = PyObject_IsTrue(result);
    Py_DECREF(result);

    if ok == 1 { 0 } else { 1 }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_compile_bytecode_full_args() {
        let argv: Vec<String> = [
            "fang-runtime",
            "--fang-tool",
            "compile-bytecode",
            "--python-version",
            "3.12",
            "--in-place",
            "/tmp/staging",
        ]
        .iter()
        .map(|s| s.to_string())
        .collect();
        let args = parse_tool_args(&argv).expect("should parse");
        assert!(
            matches!(&args.command, ToolCommand::CompileBytecode { dir } if dir == "/tmp/staging")
        );
        assert_eq!(args.python_version.as_deref(), Some("3.12"));
    }

    #[test]
    fn parse_compile_bytecode_no_version() {
        let argv: Vec<String> =
            ["fang-runtime", "--fang-tool", "compile-bytecode", "--in-place", "/tmp/staging"]
                .iter()
                .map(|s| s.to_string())
                .collect();
        let args = parse_tool_args(&argv).expect("should parse");
        assert!(matches!(args.command, ToolCommand::CompileBytecode { .. }));
        assert!(args.python_version.is_none());
    }

    #[test]
    fn parse_no_fang_tool_flag_returns_none() {
        let argv: Vec<String> = ["fang-runtime", "--other-flag"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        assert!(parse_tool_args(&argv).is_none());
    }

    #[test]
    fn parse_unknown_command_returns_none() {
        let argv: Vec<String> = ["fang-runtime", "--fang-tool", "unknown-command"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        assert!(parse_tool_args(&argv).is_none());
    }

    #[test]
    fn parse_missing_in_place_returns_none() {
        let argv: Vec<String> =
            ["fang-runtime", "--fang-tool", "compile-bytecode", "--python-version", "3.12"]
                .iter()
                .map(|s| s.to_string())
                .collect();
        assert!(parse_tool_args(&argv).is_none());
    }

    #[test]
    fn parse_flag_order_independent() {
        // --in-place before --python-version
        let argv: Vec<String> = [
            "fang-runtime",
            "--fang-tool",
            "compile-bytecode",
            "--in-place",
            "/tmp/out",
            "--python-version",
            "3.11",
        ]
        .iter()
        .map(|s| s.to_string())
        .collect();
        let args = parse_tool_args(&argv).expect("should parse");
        assert!(
            matches!(&args.command, ToolCommand::CompileBytecode { dir } if dir == "/tmp/out")
        );
        assert_eq!(args.python_version.as_deref(), Some("3.11"));
    }
}
