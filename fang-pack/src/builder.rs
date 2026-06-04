use std::collections::HashSet;

use crate::error::{Error, Result};
use crate::format::{Header, IndexEntry, HEADER_SIZE};
use crate::meta::Meta;
use crate::path::has_traversal;

const VALID_PREFIXES: &[&str] = &[
    "stdlib/",
    "site-packages/",
    "extensions/",
    "native-libs/",
    "app/",
    "meta/",
];

fn validate_path(path: &str) -> Result<()> {
    if has_traversal(path) {
        return Err(Error::InvalidPath(path.to_string()));
    }
    if VALID_PREFIXES.iter().any(|p| path.starts_with(p)) {
        Ok(())
    } else {
        Err(Error::InvalidPath(path.to_string()))
    }
}

pub struct ArchiveBuilder {
    blobs: Vec<(String, Vec<u8>)>,
    seen: HashSet<String>,
    meta: Option<Meta>,
    compression_level: i32,
}

impl ArchiveBuilder {
    pub fn new() -> Self {
        Self {
            blobs: Vec::new(),
            seen: HashSet::new(),
            meta: None,
            compression_level: 3,
        }
    }

    pub fn add(&mut self, path: &str, data: &[u8]) -> Result<()> {
        validate_path(path)?;
        if !self.seen.insert(path.to_string()) {
            return Err(Error::DuplicatePath(path.to_string()));
        }
        self.blobs.push((path.to_string(), data.to_vec()));
        Ok(())
    }

    pub fn set_meta(&mut self, meta: Meta) -> &mut Self {
        self.meta = Some(meta);
        self
    }

    pub fn with_compression_level(&mut self, level: i32) -> &mut Self {
        self.compression_level = level;
        self
    }

    pub fn build(mut self) -> Result<Vec<u8>> {
        let meta = self.meta.take().ok_or(Error::MissingMeta)?;
        let meta_json = serde_json::to_vec(&meta)?;
        self.blobs.push(("meta/meta.json".to_string(), meta_json));

        let mut blob_region: Vec<u8> = Vec::new();
        let mut entries: Vec<IndexEntry> = Vec::with_capacity(self.blobs.len());

        for (path, data) in &self.blobs {
            let content_hash: [u8; 32] = *blake3::hash(data).as_bytes();
            let offset = blob_region.len() as u64;
            let compressed = zstd::encode_all(data.as_slice(), self.compression_level)
                .map_err(|e| Error::DecompressionError(e.to_string()))?;
            entries.push(IndexEntry {
                path: path.clone(),
                offset,
                compressed_size: compressed.len() as u64,
                uncompressed_size: data.len() as u64,
                content_hash,
            });
            blob_region.extend_from_slice(&compressed);
        }

        let mut raw_index: Vec<u8> = Vec::new();
        for e in &entries {
            e.serialize(&mut raw_index);
        }
        let compressed_index = zstd::encode_all(raw_index.as_slice(), self.compression_level)
            .map_err(|e| Error::DecompressionError(e.to_string()))?;

        let blob_region_offset = HEADER_SIZE as u64;
        let blob_region_length = blob_region.len() as u64;
        let index_offset = blob_region_offset + blob_region_length;

        let header = Header {
            index_offset,
            index_length: compressed_index.len() as u64,
            blob_region_offset,
            blob_region_length,
            entry_count: entries.len() as u32,
            flags: 0,
        };

        let mut out = Vec::with_capacity(HEADER_SIZE + blob_region.len() + compressed_index.len());
        header.write_to(&mut out);
        out.extend_from_slice(&blob_region);
        out.extend_from_slice(&compressed_index);
        Ok(out)
    }
}

impl Default for ArchiveBuilder {
    fn default() -> Self {
        Self::new()
    }
}
