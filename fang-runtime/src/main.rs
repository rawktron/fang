mod entrypoint;
mod frozen;
mod python_init;
mod runtime_stdlib;
mod tool_mode;

use entrypoint::{run_entrypoint, set_sys_argv};
use fang_pack::Archive;
use pyo3_ffi::*;
use python_init::{init_cpython, prewarm_stdlib_modules, setup_meta_path, validate_version};

fn main() {
    let argv: Vec<String> = std::env::args().collect();
    let program_name = argv.first().map(|s| s.as_str()).unwrap_or("fang-runtime");

    // Dispatch to build-tool mode before loading the app archive — tool mode
    // runs without an embedded archive.
    if let Some(tool_args) = tool_mode::parse_tool_args(&argv) {
        std::process::exit(tool_mode::run_tool_mode(program_name, tool_args));
    }

    // 6.1 Load archive from embedded binary section. If no archive is present
    // (bare runtime binary), run as a plain Python interpreter using the embedded
    // runtime stdlib so uv and other tools can probe version/prefix/sysconfig.
    let archive = match Archive::from_current_binary() {
        Ok(a) => a,
        Err(_) => {
            std::process::exit(tool_mode::run_plain_interpreter(program_name, &argv));
        }
    };

    // 6.2 Read entrypoint and python version from archive metadata
    let meta = archive.meta().unwrap_or_else(|e| {
        eprintln!("fang: failed to read archive metadata: {e}");
        std::process::exit(1);
    });

    unsafe {
        // 6.3 Initialize CPython in isolated mode
        init_cpython(program_name).unwrap_or_else(|e| {
            eprintln!("{e}");
            std::process::exit(1);
        });

        // 6.4 Validate Python version matches the archive
        validate_version(&meta.python_version).unwrap_or_else(|e| {
            eprintln!("{e}");
            Py_Finalize();
            std::process::exit(1);
        });

        // 6.5 Install FangImporter, clear PathFinder, reset sys.path/prefix.
        // Must happen before prewarm_stdlib_modules: the host filesystem has no
        // /install tree, so runpy and importlib.util must come from the archive.
        let runtime_stdlib = runtime_stdlib::archive().unwrap_or_else(|e| {
            eprintln!("fang: failed to load runtime stdlib archive: {e}");
            Py_Finalize();
            std::process::exit(1);
        });

        let archive_ptr = Box::into_raw(Box::new(archive));
        let runtime_stdlib_ptr = Box::into_raw(Box::new(runtime_stdlib));
        setup_meta_path(archive_ptr, runtime_stdlib_ptr, &meta).unwrap_or_else(|e| {
            eprintln!("fang: {e}");
            Py_Finalize();
            std::process::exit(1);
        });

        // 6.6 Pre-warm importlib.util + runpy via FangImporter (archive stdlib).
        prewarm_stdlib_modules().unwrap_or_else(|e| {
            eprintln!("{e}");
            Py_Finalize();
            std::process::exit(1);
        });

        // 6.7 Wire sys.argv
        set_sys_argv(&argv);

        // 6.8 Run the entrypoint module
        let exit_code = run_entrypoint(&meta.entry_point, meta.entry_callable.as_deref());

        // 6.9 Finalize and exit
        Py_Finalize();
        std::process::exit(exit_code);
    }
}
