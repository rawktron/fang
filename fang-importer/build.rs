fn main() {
    // Extension modules on macOS must allow undefined Python symbols to be
    // resolved at dlopen time by the hosting Python process.
    if std::env::var("CARGO_CFG_TARGET_OS").unwrap_or_default() == "macos" {
        if std::env::var("CARGO_FEATURE_EXTENSION_MODULE").is_ok() {
            println!("cargo:rustc-link-arg=-undefined");
            println!("cargo:rustc-link-arg=dynamic_lookup");
        }
    }
}
