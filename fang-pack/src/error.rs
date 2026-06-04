use thiserror::Error;

#[derive(Debug, Error)]
pub enum Error {
    #[error("invalid magic bytes — not a FANG archive")]
    InvalidMagic,
    #[error("unsupported archive version: {0}")]
    UnsupportedVersion(u8),
    #[error("corrupt index: {0}")]
    CorruptIndex(String),
    #[error("invalid asset path '{0}': must begin with stdlib/, site-packages/, extensions/, native-libs/, app/, or meta/")]
    InvalidPath(String),
    #[error("duplicate asset path: {0}")]
    DuplicatePath(String),
    #[error("meta not set; call set_meta() before build()")]
    MissingMeta,
    #[error("FANG asset section not found in current binary")]
    SectionNotFound,
    #[error("archive data is truncated")]
    TruncatedArchive,
    #[error("content hash mismatch for '{path}'")]
    HashMismatch { path: String },
    #[error("decompression error: {0}")]
    DecompressionError(String),
    #[error("missing manifest: no meta/meta.json in archive")]
    MissingManifest,
    #[error("JSON error: {0}")]
    Json(#[from] serde_json::Error),
}

pub type Result<T> = std::result::Result<T, Error>;
