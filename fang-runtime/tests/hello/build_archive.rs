/// Builds tests/hello/hello.fang.
/// Usage: build_archive <init_pyc_path> <main_pyc_path> <archive_out_path> <python_version>
fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 5 {
        eprintln!("usage: build_archive <init_pyc> <main_pyc> <archive_out> <python_version>");
        std::process::exit(1);
    }
    let init_pyc_path = &args[1];
    let main_pyc_path = &args[2];
    let archive_out = &args[3];
    let python_version = &args[4];

    let init_bytes = std::fs::read(init_pyc_path).expect("read __init__.pyc");
    let main_bytes = std::fs::read(main_pyc_path).expect("read __main__.pyc");

    let mut b = fang_pack::ArchiveBuilder::new();
    // Package `hello` → app/hello/__init__.pyc
    b.add("app/hello/__init__.pyc", &init_bytes)
        .expect("add __init__");
    // Module `hello.__main__` → app/hello/__main__.pyc
    b.add("app/hello/__main__.pyc", &main_bytes)
        .expect("add __main__");
    b.set_meta(fang_pack::Meta {
        python_version: python_version.to_string(),
        entry_point: "hello.__main__".to_string(),
        entry_callable: None,
        platform: std::env::consts::OS.to_string(),
        build_timestamp: "2026-05-09T00:00:00Z".to_string(),
        project_name: "hello".to_string(),
        extensions: std::collections::HashMap::new(),
        native_libs: Vec::new(),
        rtld_global: true,
    });
    let bytes = b.build().expect("build archive");
    std::fs::write(archive_out, &bytes).expect("write archive");
    println!("wrote {} bytes to {}", bytes.len(), archive_out);
}
