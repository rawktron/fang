pub fn module_name_to_paths(fullname: &str) -> Vec<String> {
    module_name_to_prefixed_paths(fullname, &["app", "site-packages", "stdlib"])
}

pub fn module_name_to_prefixed_paths(fullname: &str, prefixes: &[&str]) -> Vec<String> {
    let path = fullname.replace('.', "/");
    let mut candidates = Vec::new();
    for prefix in prefixes {
        candidates.push(format!("{}/{}.pyc", prefix, path));
        candidates.push(format!("{}/{}/__init__.pyc", prefix, path));
    }
    candidates
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn top_level_module() {
        let paths = module_name_to_paths("os");
        assert_eq!(paths[0], "app/os.pyc");
        assert_eq!(paths[1], "app/os/__init__.pyc");
        assert_eq!(paths[2], "site-packages/os.pyc");
        assert_eq!(paths[4], "stdlib/os.pyc");
    }

    #[test]
    fn submodule() {
        let paths = module_name_to_paths("urllib.parse");
        assert!(paths.contains(&"stdlib/urllib/parse.pyc".to_string()));
        assert!(paths.contains(&"stdlib/urllib/parse/__init__.pyc".to_string()));
    }

    #[test]
    fn package() {
        let paths = module_name_to_paths("click");
        assert_eq!(paths[0], "app/click.pyc");
        assert_eq!(paths[1], "app/click/__init__.pyc");
        assert!(paths.contains(&"site-packages/click/__init__.pyc".to_string()));
    }

    #[test]
    fn priority_order() {
        let paths = module_name_to_paths("mylib");
        // app comes before site-packages comes before stdlib
        let app_idx = paths.iter().position(|p| p.starts_with("app/")).unwrap();
        let sp_idx = paths
            .iter()
            .position(|p| p.starts_with("site-packages/"))
            .unwrap();
        let stdlib_idx = paths.iter().position(|p| p.starts_with("stdlib/")).unwrap();
        assert!(app_idx < sp_idx);
        assert!(sp_idx < stdlib_idx);
    }

    #[test]
    fn module_before_init_within_prefix() {
        let paths = module_name_to_paths("os");
        let stdlib_module = paths.iter().position(|p| p == "stdlib/os.pyc").unwrap();
        let stdlib_init = paths
            .iter()
            .position(|p| p == "stdlib/os/__init__.pyc")
            .unwrap();
        assert!(stdlib_module < stdlib_init);
    }
}
