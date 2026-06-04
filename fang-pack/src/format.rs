use crate::error::{Error, Result};

pub const MAGIC: &[u8; 4] = b"FANG";
pub const VERSION: u8 = 1;
pub const HEADER_SIZE: usize = 64;

// 64-byte fixed header layout (all integers little-endian):
//  0.. 4  magic b"FANG"
//  4      version u8
//  5.. 8  reserved [u8; 3]
//  8..16  index_offset u64
// 16..24  index_length u64
// 24..32  blob_region_offset u64
// 32..40  blob_region_length u64
// 40..44  entry_count u32
// 44..48  flags u32
// 48..64  reserved [u8; 16]
#[derive(Debug)]
pub struct Header {
    pub index_offset: u64,
    pub index_length: u64,
    pub blob_region_offset: u64,
    pub blob_region_length: u64,
    pub entry_count: u32,
    pub flags: u32,
}

impl Header {
    pub fn read_from(data: &[u8]) -> Result<Self> {
        if data.len() < HEADER_SIZE {
            return Err(Error::TruncatedArchive);
        }
        if &data[0..4] != MAGIC {
            return Err(Error::InvalidMagic);
        }
        let version = data[4];
        if version != VERSION {
            return Err(Error::UnsupportedVersion(version));
        }
        Ok(Self {
            index_offset: u64::from_le_bytes(data[8..16].try_into().unwrap()),
            index_length: u64::from_le_bytes(data[16..24].try_into().unwrap()),
            blob_region_offset: u64::from_le_bytes(data[24..32].try_into().unwrap()),
            blob_region_length: u64::from_le_bytes(data[32..40].try_into().unwrap()),
            entry_count: u32::from_le_bytes(data[40..44].try_into().unwrap()),
            flags: u32::from_le_bytes(data[44..48].try_into().unwrap()),
        })
    }

    pub fn write_to(&self, buf: &mut Vec<u8>) {
        buf.extend_from_slice(MAGIC);
        buf.push(VERSION);
        buf.extend_from_slice(&[0u8; 3]);
        buf.extend_from_slice(&self.index_offset.to_le_bytes());
        buf.extend_from_slice(&self.index_length.to_le_bytes());
        buf.extend_from_slice(&self.blob_region_offset.to_le_bytes());
        buf.extend_from_slice(&self.blob_region_length.to_le_bytes());
        buf.extend_from_slice(&self.entry_count.to_le_bytes());
        buf.extend_from_slice(&self.flags.to_le_bytes());
        buf.extend_from_slice(&[0u8; 16]);
    }
}

// Variable-length binary format per entry:
//  u16      path_len
//  [u8]     path (UTF-8, path_len bytes)
//  u64      offset (bytes from blob_region_offset)
//  u64      compressed_size
//  u64      uncompressed_size
//  [u8; 32] content_hash (BLAKE3)
#[derive(Debug, Clone)]
pub struct IndexEntry {
    pub path: String,
    pub offset: u64,
    pub compressed_size: u64,
    pub uncompressed_size: u64,
    pub content_hash: [u8; 32],
}

impl IndexEntry {
    pub fn serialize(&self, buf: &mut Vec<u8>) {
        let path_bytes = self.path.as_bytes();
        buf.extend_from_slice(&(path_bytes.len() as u16).to_le_bytes());
        buf.extend_from_slice(path_bytes);
        buf.extend_from_slice(&self.offset.to_le_bytes());
        buf.extend_from_slice(&self.compressed_size.to_le_bytes());
        buf.extend_from_slice(&self.uncompressed_size.to_le_bytes());
        buf.extend_from_slice(&self.content_hash);
    }

    pub fn deserialize_all(data: &[u8], count: usize) -> Result<Vec<Self>> {
        // Minimum bytes per serialized entry: 2 (path_len u16) + 8+8+8+32 (fixed fields)
        const MIN_ENTRY_BYTES: usize = 60;
        let max_possible = data.len() / MIN_ENTRY_BYTES;
        if count > max_possible {
            return Err(Error::CorruptIndex(format!(
                "entry_count {count} exceeds what {}-byte index can hold",
                data.len()
            )));
        }
        let mut entries = Vec::with_capacity(count);
        let mut pos = 0;
        for i in 0..count {
            if pos + 2 > data.len() {
                return Err(Error::CorruptIndex(format!("truncated at entry {i}")));
            }
            let path_len = u16::from_le_bytes([data[pos], data[pos + 1]]) as usize;
            pos += 2;

            if pos + path_len > data.len() {
                return Err(Error::CorruptIndex(format!("path truncated at entry {i}")));
            }
            let path = std::str::from_utf8(&data[pos..pos + path_len])
                .map_err(|_| Error::CorruptIndex(format!("invalid UTF-8 at entry {i}")))?
                .to_string();
            pos += path_len;

            const FIXED: usize = 8 + 8 + 8 + 32;
            if pos + FIXED > data.len() {
                return Err(Error::CorruptIndex(format!(
                    "fields truncated at entry {i}"
                )));
            }
            let offset = u64::from_le_bytes(data[pos..pos + 8].try_into().unwrap());
            pos += 8;
            let compressed_size = u64::from_le_bytes(data[pos..pos + 8].try_into().unwrap());
            pos += 8;
            let uncompressed_size = u64::from_le_bytes(data[pos..pos + 8].try_into().unwrap());
            pos += 8;
            let mut content_hash = [0u8; 32];
            content_hash.copy_from_slice(&data[pos..pos + 32]);
            pos += 32;

            entries.push(IndexEntry {
                path,
                offset,
                compressed_size,
                uncompressed_size,
                content_hash,
            });
        }
        Ok(entries)
    }
}
