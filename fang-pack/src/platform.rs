// ── Linux ─────────────────────────────────────────────────────────────────────

#[cfg(target_os = "linux")]
pub fn section_bytes() -> crate::Result<Vec<u8>> {
    let exe_path = std::env::current_exe().map_err(|_| crate::Error::SectionNotFound)?;
    let data = std::fs::read(&exe_path).map_err(|_| crate::Error::SectionNotFound)?;

    // Try the ELF section first (compile-time embedded via FANG_ARCHIVE).
    if let Ok(data) = extract_elf_section(&data) {
        return Ok(data);
    }

    // Fall back to FANGPACK trailer (Python fang build output).
    extract_trailer(&data)
}

#[cfg(target_os = "linux")]
fn extract_elf_section(data: &[u8]) -> crate::Result<Vec<u8>> {
    const ELF_HEADER_SIZE: usize = 64;
    const ELF_MAGIC: &[u8; 4] = b"\x7fELF";
    const ELF_CLASS_64: u8 = 2;
    const ELF_DATA_LE: u8 = 1;
    const SH_NAME_OFFSET: usize = 0;
    const SH_OFFSET_OFFSET: usize = 24;
    const SH_SIZE_OFFSET: usize = 32;

    if data.len() < ELF_HEADER_SIZE
        || &data[0..4] != ELF_MAGIC
        || data[4] != ELF_CLASS_64
        || data[5] != ELF_DATA_LE
    {
        return Err(crate::Error::SectionNotFound);
    }

    let section_header_offset = read_u64(data, 40)? as usize;
    let section_header_size = read_u16(data, 58)? as usize;
    let section_count = read_u16(data, 60)? as usize;
    let section_name_index = read_u16(data, 62)? as usize;

    if section_header_size < ELF_HEADER_SIZE
        || section_count == 0
        || section_name_index >= section_count
    {
        return Err(crate::Error::SectionNotFound);
    }
    let section_headers_size = section_count
        .checked_mul(section_header_size)
        .ok_or(crate::Error::SectionNotFound)?;
    if section_header_offset
        .checked_add(section_headers_size)
        .filter(|end| *end <= data.len())
        .is_none()
    {
        return Err(crate::Error::SectionNotFound);
    }

    let shstr_header = section_header_offset + section_name_index * section_header_size;
    let shstr_offset = read_u64(data, shstr_header + SH_OFFSET_OFFSET)? as usize;
    let shstr_size = read_u64(data, shstr_header + SH_SIZE_OFFSET)? as usize;
    let shstr = data
        .get(shstr_offset..shstr_offset.saturating_add(shstr_size))
        .ok_or(crate::Error::SectionNotFound)?;

    for i in 0..section_count {
        let header = section_header_offset + i * section_header_size;
        let name_offset = read_u32(data, header + SH_NAME_OFFSET)? as usize;
        let section_offset = read_u64(data, header + SH_OFFSET_OFFSET)? as usize;
        let section_size = read_u64(data, header + SH_SIZE_OFFSET)? as usize;

        let name = section_name(shstr, name_offset)?;
        if name == b"fang_assets" || name == b".fang_assets" {
            let section = data
                .get(section_offset..section_offset.saturating_add(section_size))
                .ok_or(crate::Error::SectionNotFound)?;
            if section.is_empty() {
                return Err(crate::Error::SectionNotFound);
            }
            return Ok(section.to_vec());
        }
    }

    Err(crate::Error::SectionNotFound)
}

#[cfg(target_os = "linux")]
fn read_u16(data: &[u8], offset: usize) -> crate::Result<u16> {
    let bytes = data
        .get(offset..offset + 2)
        .ok_or(crate::Error::SectionNotFound)?;
    Ok(u16::from_le_bytes(bytes.try_into().unwrap()))
}

#[cfg(target_os = "linux")]
fn read_u32(data: &[u8], offset: usize) -> crate::Result<u32> {
    let bytes = data
        .get(offset..offset + 4)
        .ok_or(crate::Error::SectionNotFound)?;
    Ok(u32::from_le_bytes(bytes.try_into().unwrap()))
}

#[cfg(target_os = "linux")]
fn read_u64(data: &[u8], offset: usize) -> crate::Result<u64> {
    let bytes = data
        .get(offset..offset + 8)
        .ok_or(crate::Error::SectionNotFound)?;
    Ok(u64::from_le_bytes(bytes.try_into().unwrap()))
}

#[cfg(target_os = "linux")]
fn section_name(shstr: &[u8], offset: usize) -> crate::Result<&[u8]> {
    let rest = shstr.get(offset..).ok_or(crate::Error::SectionNotFound)?;
    let end = rest
        .iter()
        .position(|b| *b == 0)
        .ok_or(crate::Error::SectionNotFound)?;
    Ok(&rest[..end])
}

// ── macOS ─────────────────────────────────────────────────────────────────────

#[cfg(target_os = "macos")]
pub fn section_bytes() -> crate::Result<Vec<u8>> {
    let exe_path = std::env::current_exe().map_err(|_| crate::Error::SectionNotFound)?;
    let data = std::fs::read(&exe_path).map_err(|_| crate::Error::SectionNotFound)?;
    // Try __FANG,__assets first — produced by both Python fang build (section
    // injection) and compile-time FANG_ARCHIVE (-Wl,-sectcreate,__FANG,__assets).
    if let Ok(bytes) = extract_fang_section(&data) {
        return Ok(bytes);
    }
    // Fall back to FANGPACK trailer for legacy / dev builds.
    extract_trailer(&data)
}

/// Parse the Mach-O binary and return the contents of the __FANG,__assets section.
#[cfg(target_os = "macos")]
fn extract_fang_section(data: &[u8]) -> crate::Result<Vec<u8>> {
    const MH_MAGIC_64: u32 = 0xfeedfacf;
    const LC_SEGMENT_64: u32 = 0x19;
    const MACH_HEADER_SIZE: usize = 32;
    const SEGMENT_CMD_SIZE: usize = 72; // sizeof(segment_command_64)
    const SECTION_64_SIZE: usize = 80; // sizeof(section_64)

    if data.len() < MACH_HEADER_SIZE {
        return Err(crate::Error::SectionNotFound);
    }
    let magic = u32::from_le_bytes(data[0..4].try_into().unwrap());
    if magic != MH_MAGIC_64 {
        return Err(crate::Error::SectionNotFound);
    }
    let ncmds = u32::from_le_bytes(data[16..20].try_into().unwrap()) as usize;
    let mut offset = MACH_HEADER_SIZE;

    for _ in 0..ncmds {
        if data.len() < offset + 8 {
            break;
        }
        let cmd = u32::from_le_bytes(data[offset..offset + 4].try_into().unwrap());
        let cmdsize = u32::from_le_bytes(data[offset + 4..offset + 8].try_into().unwrap()) as usize;

        if cmd == LC_SEGMENT_64 && data.len() >= offset + SEGMENT_CMD_SIZE {
            // segname is at offset+8, 16 bytes
            if data[offset + 8..offset + 24].starts_with(b"__FANG") {
                // nsects is at offset+64 within segment_command_64
                let nsects =
                    u32::from_le_bytes(data[offset + 64..offset + 68].try_into().unwrap()) as usize;
                let mut sect_off = offset + SEGMENT_CMD_SIZE;
                for _ in 0..nsects {
                    if data.len() < sect_off + SECTION_64_SIZE {
                        break;
                    }
                    // sectname is at sect_off+0, 16 bytes
                    if data[sect_off..sect_off + 16].starts_with(b"__assets") {
                        // size   at sect_off+40 (8 bytes)
                        // offset at sect_off+48 (4 bytes)
                        let size = u64::from_le_bytes(
                            data[sect_off + 40..sect_off + 48].try_into().unwrap(),
                        ) as usize;
                        let file_offset = u32::from_le_bytes(
                            data[sect_off + 48..sect_off + 52].try_into().unwrap(),
                        ) as usize;
                        if size > 0 && file_offset + size <= data.len() {
                            return Ok(data[file_offset..file_offset + size].to_vec());
                        }
                    }
                    sect_off += SECTION_64_SIZE;
                }
            }
        }

        if cmdsize == 0 {
            break;
        }
        offset += cmdsize;
    }
    Err(crate::Error::SectionNotFound)
}

// ── FANGPACK trailer (shared by Linux and macOS) ──────────────────────────────

#[cfg(any(target_os = "linux", target_os = "macos"))]
pub(crate) fn extract_trailer(data: &[u8]) -> crate::Result<Vec<u8>> {
    const MAGIC: &[u8; 8] = b"FANGPACK";
    const TRAILER_SIZE: usize = 16;

    // On macOS, if an LC_CODE_SIGNATURE is present, the signature data was
    // appended after the Fang trailer; use its dataoff as the logical EOF.
    let logical_eof = {
        #[cfg(target_os = "macos")]
        {
            code_signature_dataoff(data).unwrap_or(data.len())
        }
        #[cfg(not(target_os = "macos"))]
        {
            data.len()
        }
    };

    if logical_eof < TRAILER_SIZE || logical_eof > data.len() {
        return Err(crate::Error::SectionNotFound);
    }

    let trailer_start = logical_eof - TRAILER_SIZE;
    if &data[trailer_start + 8..trailer_start + 16] != MAGIC {
        return Err(crate::Error::SectionNotFound);
    }

    let archive_len =
        u64::from_le_bytes(data[trailer_start..trailer_start + 8].try_into().unwrap()) as usize;

    if trailer_start < archive_len {
        return Err(crate::Error::SectionNotFound);
    }

    let archive_start = trailer_start - archive_len;
    Ok(data[archive_start..archive_start + archive_len].to_vec())
}

/// Returns `LC_CODE_SIGNATURE.dataoff` from a 64-bit thin Mach-O, if present.
#[cfg(target_os = "macos")]
fn code_signature_dataoff(data: &[u8]) -> Option<usize> {
    const MH_MAGIC_64: u32 = 0xfeedfacf;
    const LC_CODE_SIGNATURE: u32 = 0x0000_001d;
    const MACH_HEADER_64_SIZE: usize = 32;

    if data.len() < MACH_HEADER_64_SIZE {
        return None;
    }
    let magic = u32::from_le_bytes(data[0..4].try_into().ok()?);
    if magic != MH_MAGIC_64 {
        return None;
    }
    let ncmds = u32::from_le_bytes(data[16..20].try_into().ok()?) as usize;
    let mut offset = MACH_HEADER_64_SIZE;
    for _ in 0..ncmds {
        if data.len() < offset + 8 {
            return None;
        }
        let cmd = u32::from_le_bytes(data[offset..offset + 4].try_into().ok()?);
        let cmdsize = u32::from_le_bytes(data[offset + 4..offset + 8].try_into().ok()?) as usize;
        if cmd == LC_CODE_SIGNATURE {
            if data.len() < offset + 12 {
                return None;
            }
            let dataoff =
                u32::from_le_bytes(data[offset + 8..offset + 12].try_into().ok()?) as usize;
            return Some(dataoff);
        }
        if cmdsize == 0 {
            return None;
        }
        offset += cmdsize;
    }
    None
}

// ── unsupported platforms ─────────────────────────────────────────────────────

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
pub fn section_bytes() -> crate::Result<Vec<u8>> {
    Err(crate::Error::SectionNotFound)
}

// ── tests ─────────────────────────────────────────────────────────────────────

#[cfg(all(test, target_os = "macos"))]
mod tests {
    use super::{code_signature_dataoff, extract_fang_section, extract_trailer};
    use crate::Error;

    // ── extract_trailer tests ─────────────────────────────────────────────────

    #[test]
    fn magic_mismatch_returns_section_not_found() {
        let mut data = vec![0u8; 100];
        data[92..100].copy_from_slice(b"WRONGMAG");
        assert!(matches!(
            extract_trailer(&data),
            Err(Error::SectionNotFound)
        ));
    }

    #[test]
    fn too_short_returns_section_not_found() {
        let data = vec![0u8; 10];
        assert!(matches!(
            extract_trailer(&data),
            Err(Error::SectionNotFound)
        ));
    }

    #[test]
    fn valid_trailer_at_physical_eof() {
        let archive = b"hello archive data";
        let archive_len = archive.len() as u64;

        let mut data = Vec::new();
        data.extend_from_slice(b"binary preamble bytes here");
        data.extend_from_slice(archive);
        data.extend_from_slice(&archive_len.to_le_bytes());
        data.extend_from_slice(b"FANGPACK");

        assert_eq!(extract_trailer(&data).unwrap(), archive);
    }

    #[test]
    fn archive_len_too_large_returns_section_not_found() {
        let mut data = vec![0u8; 100];
        let bad_len: u64 = 9999;
        data[84..92].copy_from_slice(&bad_len.to_le_bytes());
        data[92..100].copy_from_slice(b"FANGPACK");
        assert!(matches!(
            extract_trailer(&data),
            Err(Error::SectionNotFound)
        ));
    }

    // ── extract_fang_section tests ────────────────────────────────────────────

    fn make_fang_macho(archive: &[u8]) -> Vec<u8> {
        const MH_MAGIC_64: u32 = 0xfeedfacf;
        const LC_SEGMENT_64: u32 = 0x19;
        const HEADER_SIZE: usize = 32;
        const SEG_SIZE: usize = 72;
        const SECT_SIZE: usize = 80;
        let lc_size = SEG_SIZE + SECT_SIZE; // 152
        let archive_offset = HEADER_SIZE + lc_size;

        let mut data = Vec::new();

        // mach_header_64 (32 bytes)
        data.extend_from_slice(&MH_MAGIC_64.to_le_bytes());
        data.extend_from_slice(&0u32.to_le_bytes()); // cputype
        data.extend_from_slice(&0u32.to_le_bytes()); // cpusubtype
        data.extend_from_slice(&2u32.to_le_bytes()); // MH_EXECUTE
        data.extend_from_slice(&1u32.to_le_bytes()); // ncmds
        data.extend_from_slice(&(lc_size as u32).to_le_bytes()); // sizeofcmds
        data.extend_from_slice(&0u32.to_le_bytes()); // flags
        data.extend_from_slice(&0u32.to_le_bytes()); // reserved

        let mut segname = [0u8; 16];
        segname[..6].copy_from_slice(b"__FANG");

        // segment_command_64 (72 bytes)
        data.extend_from_slice(&LC_SEGMENT_64.to_le_bytes());
        data.extend_from_slice(&(lc_size as u32).to_le_bytes());
        data.extend_from_slice(&segname);
        data.extend_from_slice(&0u64.to_le_bytes()); // vmaddr
        data.extend_from_slice(&(archive.len() as u64).to_le_bytes()); // vmsize
        data.extend_from_slice(&(archive_offset as u64).to_le_bytes()); // fileoff
        data.extend_from_slice(&(archive.len() as u64).to_le_bytes()); // filesize
        data.extend_from_slice(&1u32.to_le_bytes()); // maxprot
        data.extend_from_slice(&1u32.to_le_bytes()); // initprot
        data.extend_from_slice(&1u32.to_le_bytes()); // nsects
        data.extend_from_slice(&0u32.to_le_bytes()); // flags

        let mut sectname = [0u8; 16];
        sectname[..8].copy_from_slice(b"__assets");

        // section_64 (80 bytes)
        data.extend_from_slice(&sectname);
        data.extend_from_slice(&segname);
        data.extend_from_slice(&0u64.to_le_bytes()); // addr
        data.extend_from_slice(&(archive.len() as u64).to_le_bytes()); // size
        data.extend_from_slice(&(archive_offset as u32).to_le_bytes()); // offset
        data.extend_from_slice(&0u32.to_le_bytes()); // align
        data.extend_from_slice(&0u32.to_le_bytes()); // reloff
        data.extend_from_slice(&0u32.to_le_bytes()); // nreloc
        data.extend_from_slice(&0u32.to_le_bytes()); // flags
        data.extend_from_slice(&0u32.to_le_bytes()); // reserved1
        data.extend_from_slice(&0u32.to_le_bytes()); // reserved2
        data.extend_from_slice(&0u32.to_le_bytes()); // reserved3

        data.extend_from_slice(archive);
        data
    }

    #[test]
    fn fang_section_found() {
        let archive = b"hello fang archive data";
        let data = make_fang_macho(archive);
        assert_eq!(extract_fang_section(&data).unwrap(), archive as &[u8]);
    }

    #[test]
    fn fang_section_not_present_in_non_fang_macho() {
        // A minimal Mach-O without any __FANG segment
        let data = make_signed_macho(b"test");
        assert!(matches!(
            extract_fang_section(&data),
            Err(Error::SectionNotFound)
        ));
    }

    #[test]
    fn fang_section_not_present_in_non_macho() {
        let data = b"ELF binary or garbage data";
        assert!(matches!(
            extract_fang_section(data),
            Err(Error::SectionNotFound)
        ));
    }

    // ── LC_CODE_SIGNATURE + trailer tests ─────────────────────────────────────

    /// Build a minimal 64-bit Mach-O with one LC_CODE_SIGNATURE load command
    /// pointing to a fake signature blob appended after the Fang trailer.
    fn make_signed_macho(archive: &[u8]) -> Vec<u8> {
        const MH_MAGIC_64: u32 = 0xfeedfacf;
        const LC_CODE_SIGNATURE: u32 = 0x0000_001d;
        const MACH_HEADER_64_SIZE: usize = 32;
        const LC_CODE_SIGNATURE_SIZE: usize = 16;

        let payload = b"fake runtime code";
        let archive_len = archive.len() as u64;
        let fake_sig = b"fake codesig data";

        let sig_offset =
            MACH_HEADER_64_SIZE + LC_CODE_SIGNATURE_SIZE + payload.len() + archive.len() + 16;

        let mut data = Vec::new();

        data.extend_from_slice(&MH_MAGIC_64.to_le_bytes());
        data.extend_from_slice(&0x0100_000cu32.to_le_bytes()); // ARM64
        data.extend_from_slice(&0u32.to_le_bytes());
        data.extend_from_slice(&2u32.to_le_bytes()); // MH_EXECUTE
        data.extend_from_slice(&1u32.to_le_bytes()); // ncmds = 1
        data.extend_from_slice(&(LC_CODE_SIGNATURE_SIZE as u32).to_le_bytes());
        data.extend_from_slice(&0u32.to_le_bytes());
        data.extend_from_slice(&0u32.to_le_bytes());

        data.extend_from_slice(&LC_CODE_SIGNATURE.to_le_bytes());
        data.extend_from_slice(&(LC_CODE_SIGNATURE_SIZE as u32).to_le_bytes());
        data.extend_from_slice(&(sig_offset as u32).to_le_bytes());
        data.extend_from_slice(&(fake_sig.len() as u32).to_le_bytes());

        data.extend_from_slice(payload);

        data.extend_from_slice(archive);
        data.extend_from_slice(&archive_len.to_le_bytes());
        data.extend_from_slice(b"FANGPACK");

        data.extend_from_slice(fake_sig);

        data
    }

    #[test]
    fn trailer_before_code_signature_is_found() {
        let archive = b"signed archive bytes";
        let data = make_signed_macho(archive);
        assert_eq!(extract_trailer(&data).unwrap(), archive as &[u8]);
    }

    #[test]
    fn code_signature_dataoff_is_parsed() {
        let archive = b"x";
        let data = make_signed_macho(archive);
        let dataoff = code_signature_dataoff(&data).expect("should find LC_CODE_SIGNATURE");
        assert!(dataoff > 0 && dataoff < data.len());
        assert_eq!(&data[dataoff..], b"fake codesig data");
    }

    #[test]
    fn no_lc_code_signature_returns_none() {
        let data = b"not a mach-o binary at all";
        assert!(code_signature_dataoff(data).is_none());
    }
}
