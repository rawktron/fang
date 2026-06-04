use std::path::{Component, Path};

/// Returns true if `path` contains any `..` or absolute (`/`) component.
/// Used to reject traversal attacks in archive paths at both read and write time.
pub fn has_traversal(path: &str) -> bool {
    Path::new(path)
        .components()
        .any(|c| matches!(c, Component::ParentDir | Component::RootDir))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normal_paths_pass() {
        assert!(!has_traversal("extensions/numpy/core/_foo.so"));
        assert!(!has_traversal("stdlib/os.pyc"));
        assert!(!has_traversal("app/main.pyc"));
    }

    #[test]
    fn parent_dir_detected() {
        assert!(has_traversal("extensions/../authorized_keys"));
        assert!(has_traversal("../etc/passwd"));
        assert!(has_traversal("stdlib/../../etc/shadow"));
    }

    #[test]
    fn absolute_path_detected() {
        assert!(has_traversal("/etc/passwd"));
        assert!(has_traversal("/tmp/evil.so"));
    }
}
