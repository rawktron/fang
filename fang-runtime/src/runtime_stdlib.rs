use fang_pack::Archive;

static RUNTIME_STDLIB_ARCHIVE: &[u8] =
    include_bytes!(concat!(env!("OUT_DIR"), "/cpython/runtime_stdlib.fang"));

pub fn archive() -> fang_pack::Result<Archive> {
    Archive::from_bytes(RUNTIME_STDLIB_ARCHIVE)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn runtime_stdlib_archive_contains_stdlib_sources() {
        let archive = archive().unwrap();
        assert!(archive.contains("stdlib/os.py"));
        assert!(archive.contains("stdlib/urllib/parse.py"));
        assert!(!archive.contains("stdlib/test/test_os.py"));
        assert!(!archive.contains("stdlib/site-packages/pip/__init__.py"));
        assert!(!archive.contains("stdlib/idlelib/__init__.py"));
        assert!(!archive.contains("stdlib/tkinter/__init__.py"));
        let meta = archive.meta().unwrap();
        assert!(meta.project_name.contains("fang-runtime-stdlib"));
    }
}
