use anyhow::Result;

// Re-export core library
pub use gradbot::{
    self, DEFAULT_GRADIUM_BASE_URL, Event, GradbotClients, Lang, Llm, LlmConfig, MsgIn, MsgOut,
    SessionConfig, SessionInputHandle, SessionOutputHandle, SttClient, TtsClient, run,
    start_session,
};

// Server implementations
pub mod openai_protocol;
pub mod openai_server;
pub mod twilio_protocol;
pub mod twilio_server;

/// Replace environment variables in a string (e.g. "$HOME/path" -> "/Users/foo/path")
fn replace_env_vars(input: &str) -> String {
    let re = regex::Regex::new(r"\$([A-Za-z_][A-Za-z0-9_]*)").unwrap();
    re.replace_all(input, |caps: &regex::Captures| {
        let var_name = &caps[1];
        std::env::var(var_name).unwrap_or_else(|_| "".to_string())
    })
    .to_string()
}

#[derive(Debug, serde::Deserialize, Clone)]
#[serde(rename_all = "snake_case")]
pub enum Transport {
    #[serde(rename = "ws-openai")]
    WsOpenai,
    Twilio(TwilioConfig),
}

#[derive(Debug, serde::Deserialize, Clone)]
pub struct TwilioConfig {
    #[serde(default)]
    pub voice_id: Option<String>,
    pub system_prompt: String,
    #[serde(default = "default_lang")]
    pub language: Lang,
}

fn default_lang() -> Lang {
    Lang::En
}

#[derive(Debug, serde::Deserialize, Clone)]
pub struct Config {
    pub log_dir: String,
    pub addr: String,
    pub port: u16,
    pub instance_name: String,
    pub gradium_api_key: String,
    pub gradium_base_url: String,
    pub llm_base_url: Option<String>,
    pub static_dir: Option<String>,
    #[serde(default)]
    pub log_sessions: bool,
    pub max_completion_tokens: Option<u32>,
    pub transport: Transport,
}

impl Config {
    pub fn load<P: AsRef<std::path::Path>>(p: P) -> Result<Self> {
        let rev = replace_env_vars;

        if let Some(parent) = p.as_ref().parent() {
            // Set CONFIG_DIR so that it can be used in the config file.
            unsafe {
                std::env::set_var("CONFIG_DIR", parent.to_string_lossy().to_string());
            }
        }
        let config = std::fs::read_to_string(p)?;
        let mut config: Self = toml::from_str(&config)?;
        config.log_dir = rev(&config.log_dir);
        config.addr = rev(&config.addr);
        config.instance_name = rev(&config.instance_name);
        config.gradium_api_key = rev(&config.gradium_api_key);
        config.gradium_base_url = rev(&config.gradium_base_url);
        if let Some(static_dir) = config.static_dir.as_mut() {
            *static_dir = rev(static_dir);
        }
        if let Transport::Twilio(twilio) = &mut config.transport {
            twilio.system_prompt = rev(&twilio.system_prompt);
        }
        Ok(config)
    }
}
