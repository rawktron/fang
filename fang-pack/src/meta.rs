use std::collections::HashMap;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Meta {
    pub python_version: String,
    pub entry_point: String,
    #[serde(default)]
    pub entry_callable: Option<String>,
    pub platform: String,
    pub build_timestamp: String,
    #[serde(default)]
    pub project_name: String,
    #[serde(default)]
    pub extensions: HashMap<String, String>,
    #[serde(default)]
    pub native_libs: Vec<String>,
    #[serde(default = "default_rtld_global")]
    pub rtld_global: bool,
}

fn default_rtld_global() -> bool {
    true
}
