mod archive;
mod builder;
mod error;
mod format;
mod meta;
mod path;
mod platform;

pub use archive::Archive;
pub use builder::ArchiveBuilder;
pub use error::{Error, Result};
pub use meta::Meta;
pub use path::has_traversal;

#[cfg(test)]
mod tests {
    use super::*;

    fn test_meta() -> Meta {
        Meta {
            python_version: "3.12.0".into(),
            entry_point: "myapp.__main__".into(),
            entry_callable: None,
            platform: "macos-arm64".into(),
            build_timestamp: "2026-05-08T00:00:00Z".into(),
            project_name: "myapp".into(),
            extensions: std::collections::HashMap::new(),
            native_libs: Vec::new(),
            rtld_global: true,
        }
    }

    fn build_with_meta(builder: &mut ArchiveBuilder) {
        builder.set_meta(test_meta());
    }

    // 5.1 — single blob round-trip
    #[test]
    fn round_trip_single_blob() {
        let mut b = ArchiveBuilder::new();
        b.add("app/main.pyc", b"hello world").unwrap();
        build_with_meta(&mut b);
        let bytes = b.build().unwrap();

        let archive = Archive::from_bytes(&bytes).unwrap();
        let got = archive.get("app/main.pyc").unwrap().unwrap();
        assert_eq!(got, b"hello world");
    }

    // 5.2 — multiple blobs across all six categories
    #[test]
    fn round_trip_all_categories() {
        let cases = [
            ("stdlib/os.pyc", b"os bytecode" as &[u8]),
            ("site-packages/click/__init__.pyc", b"click bytecode"),
            ("extensions/numpy.core._multiarray.so", b"native blob"),
            ("native-libs/libSDL2.so", b"sdl blob"),
            ("app/myapp/__main__.pyc", b"app bytecode"),
            ("meta/extra.json", b"{\"key\":\"value\"}"),
        ];

        let mut b = ArchiveBuilder::new();
        for (path, data) in &cases {
            b.add(path, data).unwrap();
        }
        build_with_meta(&mut b);
        let bytes = b.build().unwrap();

        let archive = Archive::from_bytes(&bytes).unwrap();
        for (path, expected) in &cases {
            let got = archive.get(path).unwrap().unwrap();
            assert_eq!(got, *expected, "mismatch for {path}");
        }
    }

    // 5.3 — duplicate path returns DuplicatePath
    #[test]
    fn duplicate_path_error() {
        let mut b = ArchiveBuilder::new();
        b.add("app/foo.pyc", b"data").unwrap();
        let err = b.add("app/foo.pyc", b"data2").unwrap_err();
        assert!(matches!(err, Error::DuplicatePath(_)));
    }

    // 5.4 — invalid category prefix returns InvalidPath
    #[test]
    fn invalid_category_prefix() {
        let mut b = ArchiveBuilder::new();
        let err = b.add("unknown/foo.pyc", b"data").unwrap_err();
        assert!(matches!(err, Error::InvalidPath(_)));

        let err2 = b.add("foo.pyc", b"data").unwrap_err();
        assert!(matches!(err2, Error::InvalidPath(_)));
    }

    // security: traversal paths rejected at add time
    #[test]
    fn traversal_path_rejected_at_add() {
        let mut b = ArchiveBuilder::new();
        let err = b.add("extensions/../authorized_keys", b"evil").unwrap_err();
        assert!(matches!(err, Error::InvalidPath(_)));

        let err2 = b.add("/etc/passwd", b"evil").unwrap_err();
        assert!(matches!(err2, Error::InvalidPath(_)));
    }

    // security: traversal paths return None from get()
    #[test]
    fn traversal_path_returns_none_from_get() {
        let mut b = ArchiveBuilder::new();
        b.add("app/main.pyc", b"data").unwrap();
        build_with_meta(&mut b);
        let bytes = b.build().unwrap();
        let archive = Archive::from_bytes(&bytes).unwrap();

        assert!(archive.get("extensions/../authorized_keys").is_none());
        assert!(archive.get("/etc/passwd").is_none());
    }

    // security: implausibly large entry_count returns CorruptIndex without OOM
    #[test]
    fn entry_count_oom_cap() {
        use crate::format::IndexEntry;
        let tiny_data = vec![0u8; 200];
        let err = IndexEntry::deserialize_all(&tiny_data, usize::MAX).unwrap_err();
        assert!(matches!(err, Error::CorruptIndex(_)));
    }

    // security: normal nested extension path still accepted
    #[test]
    fn normal_extension_path_accepted() {
        let mut b = ArchiveBuilder::new();
        b.add(
            "extensions/numpy/core/_multiarray_umath.cpython-312-x86_64-linux-gnu.so",
            b"blob",
        )
        .unwrap();
        build_with_meta(&mut b);
        let bytes = b.build().unwrap();
        let archive = Archive::from_bytes(&bytes).unwrap();
        assert!(archive
            .get("extensions/numpy/core/_multiarray_umath.cpython-312-x86_64-linux-gnu.so")
            .is_some());
    }

    // 5.5 — build without set_meta returns MissingMeta
    #[test]
    fn build_without_meta_fails() {
        let mut b = ArchiveBuilder::new();
        b.add("app/main.pyc", b"data").unwrap();
        let err = b.build().unwrap_err();
        assert!(matches!(err, Error::MissingMeta));
    }

    // 5.6 — from_bytes on truncated data returns TruncatedArchive
    #[test]
    fn truncated_archive_rejected() {
        let err = Archive::from_bytes(&[0u8; 10]).unwrap_err();
        assert!(matches!(err, Error::TruncatedArchive));
    }

    // 5.7 — from_bytes on bad magic returns InvalidMagic
    #[test]
    fn bad_magic_rejected() {
        let mut data = vec![0u8; 64];
        data[0..4].copy_from_slice(b"NOPE");
        let err = Archive::from_bytes(&data).unwrap_err();
        assert!(matches!(err, Error::InvalidMagic));
    }

    // 5.8 — get_verified detects hash mismatch on tampered archive
    #[test]
    fn hash_mismatch_detected() {
        let mut b = ArchiveBuilder::new();
        b.add("app/main.pyc", b"original data").unwrap();
        build_with_meta(&mut b);
        let mut bytes = b.build().unwrap();

        // Corrupt a byte in the blob region (after the 64-byte header)
        bytes[64] ^= 0xFF;

        // The archive may fail to open (corrupt zstd frame) or detect hash mismatch.
        // Either is a valid detection of tampering.
        match Archive::from_bytes(&bytes) {
            Err(_) => {}
            Ok(archive) => {
                let result = archive.get_verified("app/main.pyc");
                match result {
                    Some(Err(Error::HashMismatch { .. }))
                    | Some(Err(Error::DecompressionError(_))) => {}
                    other => panic!(
                        "expected tamper detection, got {:?}",
                        other.map(|r| r.map(|_| "<data>"))
                    ),
                }
            }
        }
    }

    // 5.9 — meta() returns correct values
    #[test]
    fn meta_round_trip() {
        let mut b = ArchiveBuilder::new();
        build_with_meta(&mut b);
        let bytes = b.build().unwrap();

        let archive = Archive::from_bytes(&bytes).unwrap();
        let meta = archive.meta().unwrap();
        assert_eq!(meta.python_version, "3.12.0");
        assert_eq!(meta.entry_point, "myapp.__main__");
        assert_eq!(meta.platform, "macos-arm64");
    }

    // 5.9b — extension index and rtld_global round-trip
    #[test]
    fn meta_extension_index_round_trip() {
        let mut extensions = std::collections::HashMap::new();
        extensions.insert(
            "numpy.core._multiarray_umath".into(),
            "extensions/numpy/core/_multiarray_umath.cpython-312-x86_64-linux-gnu.so".into(),
        );
        let meta = Meta {
            python_version: "3.12.0".into(),
            entry_point: "myapp.__main__".into(),
            entry_callable: None,
            platform: "linux-x86_64".into(),
            build_timestamp: "2026-05-10T00:00:00Z".into(),
            project_name: "myapp".into(),
            extensions,
            native_libs: vec![
                "native-libs/pkg.libs/libprovider-hash.so.1".into(),
                "native-libs/pkg.libs/libconsumer-hash.so.1".into(),
            ],
            rtld_global: false,
        };
        let mut b = ArchiveBuilder::new();
        b.set_meta(meta);
        let bytes = b.build().unwrap();
        let archive = Archive::from_bytes(&bytes).unwrap();
        let got = archive.meta().unwrap();
        assert_eq!(
            got.extensions
                .get("numpy.core._multiarray_umath")
                .map(|s| s.as_str()),
            Some("extensions/numpy/core/_multiarray_umath.cpython-312-x86_64-linux-gnu.so")
        );
        assert!(!got.rtld_global);
        assert_eq!(got.project_name, "myapp");
        assert_eq!(
            got.native_libs,
            vec![
                "native-libs/pkg.libs/libprovider-hash.so.1",
                "native-libs/pkg.libs/libconsumer-hash.so.1"
            ]
        );
    }

    // 5.9c — old archive without extension fields deserializes with defaults
    #[test]
    fn meta_defaults_for_missing_extension_fields() {
        // Build an archive using a manually serialized meta that lacks new fields
        let old_meta_json = r#"{"python_version":"3.12.0","entry_point":"app.__main__","platform":"macos-arm64","build_timestamp":"2026-01-01T00:00:00Z"}"#;
        let mut b = ArchiveBuilder::new();
        b.add("meta/meta.json", old_meta_json.as_bytes()).unwrap();
        // set_meta would overwrite, so add raw instead and build manually
        // — just verify serde deserialization defaults work
        let m: Meta = serde_json::from_str(old_meta_json).unwrap();
        assert!(m.extensions.is_empty());
        assert!(m.native_libs.is_empty());
        assert!(m.rtld_global);
        assert_eq!(m.project_name, "");
    }

    #[test]
    fn meta_empty_native_libs_round_trip() {
        let meta = test_meta();
        let json = serde_json::to_string(&meta).unwrap();
        let got: Meta = serde_json::from_str(&json).unwrap();
        assert!(got.native_libs.is_empty());
    }

    // 5.10 — get on unknown path returns None
    #[test]
    fn unknown_path_returns_none() {
        let mut b = ArchiveBuilder::new();
        build_with_meta(&mut b);
        let bytes = b.build().unwrap();

        let archive = Archive::from_bytes(&bytes).unwrap();
        assert!(archive.get("stdlib/nonexistent.pyc").is_none());
    }
}
