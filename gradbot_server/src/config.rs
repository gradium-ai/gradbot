use crate::protocol::SessionConfigWire;
use serde::Deserialize;

fn default_addr() -> String {
    "0.0.0.0".to_string()
}

fn default_port() -> u16 {
    8080
}

fn default_log_dir() -> String {
    "./logs".to_string()
}

fn default_gradium_base_url() -> String {
    std::env::var("GRADIUM_BASE_URL")
        .unwrap_or_else(|_| gradbot::DEFAULT_GRADIUM_BASE_URL.to_string())
}

#[derive(Debug, Deserialize)]
pub struct Config {
    #[serde(default = "default_addr")]
    pub addr: String,
    #[serde(default = "default_port")]
    pub port: u16,
    #[serde(default = "default_gradium_base_url")]
    pub gradium_base_url: String,

    // LLM credentials (server-owned, pinned)
    pub llm_base_url: Option<String>,
    pub llm_api_key: Option<String>,
    pub llm_model_name: Option<String>,
    pub max_completion_tokens: Option<u32>,

    // Pinned session config fields — override any client values
    #[serde(default)]
    pub pinned: SessionConfigWire,

    // Logging
    #[serde(default = "default_log_dir")]
    pub log_dir: String,
    #[serde(default)]
    pub log_sessions: bool,
}

/// Replace `$VAR_NAME` patterns with their environment variable values.
fn replace_env_vars(input: &str) -> String {
    let re = regex::Regex::new(r"\$([A-Za-z_][A-Za-z0-9_]*)").unwrap();
    re.replace_all(input, |caps: &regex::Captures| {
        let var_name = &caps[1];
        std::env::var(var_name).unwrap_or_default()
    })
    .to_string()
}

impl Config {
    pub fn load<P: AsRef<std::path::Path>>(p: P) -> anyhow::Result<Self> {
        let rev = replace_env_vars;

        if let Some(parent) = p.as_ref().parent() {
            unsafe {
                std::env::set_var("CONFIG_DIR", parent.to_string_lossy().to_string());
            }
        }

        let text = std::fs::read_to_string(p.as_ref())?;
        let mut config: Self = toml::from_str(&text)?;

        config.addr = rev(&config.addr);
        config.gradium_base_url = rev(&config.gradium_base_url);
        config.log_dir = rev(&config.log_dir);
        if let Some(url) = config.llm_base_url.as_mut() {
            *url = rev(url);
        }
        if let Some(key) = config.llm_api_key.as_mut() {
            *key = rev(key);
        }
        if let Some(name) = config.llm_model_name.as_mut() {
            *name = rev(name);
        }

        Ok(config)
    }
}
