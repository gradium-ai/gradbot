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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn replace_env_vars_substitutes_set_variables() {
        let key = "GRADBOT_SERVER_TEST_REPLACE_ENV_VARS_SET";
        unsafe {
            std::env::set_var(key, "hello");
        }
        let out = replace_env_vars(&format!("prefix-${key}-suffix"));
        unsafe {
            std::env::remove_var(key);
        }
        assert_eq!(out, "prefix-hello-suffix");
    }

    #[test]
    fn replace_env_vars_treats_missing_variables_as_empty() {
        let key = "GRADBOT_SERVER_TEST_REPLACE_ENV_VARS_MISSING";
        unsafe {
            std::env::remove_var(key);
        }
        let out = replace_env_vars(&format!("prefix-${key}-suffix"));
        assert_eq!(out, "prefix--suffix");
    }

    #[test]
    fn replace_env_vars_leaves_plain_text_untouched() {
        let out = replace_env_vars("no variables here");
        assert_eq!(out, "no variables here");
    }

    fn write_temp_config(name: &str, contents: &str) -> std::path::PathBuf {
        let path = std::env::temp_dir().join(format!(
            "gradbot_server_test_config_{name}_{:?}.toml",
            std::thread::current().id()
        ));
        std::fs::write(&path, contents).unwrap();
        path
    }

    #[test]
    fn config_load_applies_defaults_for_missing_fields() {
        let path = write_temp_config("defaults", "");
        let config = Config::load(&path).unwrap();
        std::fs::remove_file(&path).ok();

        assert_eq!(config.addr, "0.0.0.0");
        assert_eq!(config.port, 8080);
        assert_eq!(config.log_dir, "./logs");
        assert!(!config.log_sessions);
        assert_eq!(config.llm_base_url, None);
    }

    #[test]
    fn config_load_substitutes_env_vars_in_llm_api_key() {
        let key = "GRADBOT_SERVER_TEST_CONFIG_LOAD_API_KEY";
        unsafe {
            std::env::set_var(key, "secret-value");
        }
        let path = write_temp_config("env_substitution", &format!("llm_api_key = \"${key}\"\n"));

        let config = Config::load(&path).unwrap();

        std::fs::remove_file(&path).ok();
        unsafe {
            std::env::remove_var(key);
        }

        assert_eq!(config.llm_api_key, Some("secret-value".to_string()));
    }

    #[test]
    fn config_load_respects_explicit_values() {
        let path = write_temp_config(
            "explicit",
            "addr = \"127.0.0.1\"\nport = 9999\nlog_sessions = true\n",
        );
        let config = Config::load(&path).unwrap();
        std::fs::remove_file(&path).ok();

        assert_eq!(config.addr, "127.0.0.1");
        assert_eq!(config.port, 9999);
        assert!(config.log_sessions);
    }
}
