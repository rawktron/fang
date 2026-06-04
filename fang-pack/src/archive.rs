use std::collections::HashMap;

use crate::error::{Error, Result};
use crate::format::{Header, IndexEntry};
use crate::meta::Meta;
use crate::path::has_traversal;
use crate::platform;

#[derive(Debug)]
pub struct Archive {
    data: Vec<u8>,
    index: HashMap<String, IndexEntry>,
    blob_region_offset: u64,
}

impl Archive {
    pub fn from_bytes(data: &[u8]) -> Result<Self> {
        Self::from_vec(data.to_vec())
    }

    pub fn from_vec(data: Vec<u8>) -> Result<Self> {
        let header = Header::read_from(&data)?;

        let idx_start = header.index_offset as usize;
        let idx_end = idx_start + header.index_length as usize;
        if idx_end > data.len() {
            return Err(Error::TruncatedArchive);
        }

        let raw_index = zstd::decode_all(&data[idx_start..idx_end])
            .map_err(|e| Error::DecompressionError(e.to_string()))?;

        let entries = IndexEntry::deserialize_all(&raw_index, header.entry_count as usize)?;
        if entries.len() != header.entry_count as usize {
            return Err(Error::CorruptIndex("entry count mismatch".into()));
        }

        let index = entries.into_iter().map(|e| (e.path.clone(), e)).collect();
        Ok(Self {
            data,
            index,
            blob_region_offset: header.blob_region_offset,
        })
    }

    pub fn from_current_binary() -> Result<Self> {
        Self::from_vec(platform::section_bytes()?)
    }

    pub fn contains(&self, path: &str) -> bool {
        self.index.contains_key(path)
    }

    pub fn paths(&self) -> impl Iterator<Item = &str> {
        self.index.keys().map(String::as_str)
    }

    pub fn get(&self, path: &str) -> Option<Result<Vec<u8>>> {
        if has_traversal(path) {
            return None;
        }
        let entry = self.index.get(path)?;
        Some(self.decompress(entry))
    }

    pub fn get_verified(&self, path: &str) -> Option<Result<Vec<u8>>> {
        if has_traversal(path) {
            return None;
        }
        let entry = self.index.get(path)?;
        Some(self.decompress_verified(entry))
    }

    /// Returns the BLAKE3 content hash for `path` from the index, without decompressing.
    /// Used by the macOS cache-hit verification path to compare against a cached file.
    pub fn content_hash(&self, path: &str) -> Option<[u8; 32]> {
        if has_traversal(path) {
            return None;
        }
        self.index.get(path).map(|e| e.content_hash)
    }

    pub fn meta(&self) -> Result<Meta> {
        let bytes = self.get("meta/meta.json").ok_or(Error::MissingManifest)??;
        Ok(serde_json::from_slice(&bytes)?)
    }

    fn decompress(&self, entry: &IndexEntry) -> Result<Vec<u8>> {
        let start = (self.blob_region_offset + entry.offset) as usize;
        let end = start + entry.compressed_size as usize;
        if end > self.data.len() {
            return Err(Error::TruncatedArchive);
        }
        zstd::decode_all(&self.data[start..end])
            .map_err(|e| Error::DecompressionError(e.to_string()))
    }

    fn decompress_verified(&self, entry: &IndexEntry) -> Result<Vec<u8>> {
        let data = self.decompress(entry)?;
        if *blake3::hash(&data).as_bytes() != entry.content_hash {
            return Err(Error::HashMismatch {
                path: entry.path.clone(),
            });
        }
        Ok(data)
    }
}
