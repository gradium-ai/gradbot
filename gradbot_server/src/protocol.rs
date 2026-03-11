use serde::{Deserialize, Serialize};

// ---------------------------------------------------------------------------
// Client → Server
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum ClientMessage {
    #[serde(rename = "session.config")]
    SessionConfig { config: SessionConfigWire },

    #[serde(rename = "tool_call.result")]
    ToolCallResult {
        call_id: String,
        result: serde_json::Value,
        #[serde(default)]
        is_error: bool,
    },
}

// ---------------------------------------------------------------------------
// Server → Client
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum ServerMessage {
    #[serde(rename = "session.config.applied")]
    SessionConfigApplied { pinned_fields: Vec<String> },

    #[serde(rename = "audio")]
    Audio {
        start_s: f64,
        stop_s: f64,
        turn_idx: u64,
        interrupted: bool,
    },

    #[serde(rename = "tts_text")]
    TtsText {
        text: String,
        start_s: f64,
        stop_s: f64,
        turn_idx: u64,
    },

    #[serde(rename = "stt_text")]
    SttText { text: String, start_s: f64 },

    #[serde(rename = "tool_call")]
    ToolCall {
        call_id: String,
        tool_name: String,
        args: serde_json::Value,
    },

    #[serde(rename = "event")]
    Event { time_s: f64, event: String },

    #[serde(rename = "error")]
    Error { message: String },
}

// ---------------------------------------------------------------------------
// SessionConfigWire — fully-optional mirror of gradbot::SessionConfig
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize, Deserialize, Default, Clone)]
pub struct SessionConfigWire {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub voice_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub instructions: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub language: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub assistant_speaks_first: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub silence_timeout_s: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tools: Option<Vec<ToolDefWire>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub flush_duration_s: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub padding_bonus: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rewrite_rules: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stt_extra_config: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tts_extra_config: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub llm_extra_config: Option<String>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct ToolDefWire {
    pub name: String,
    pub description: String,
    pub parameters: serde_json::Value,
}

// ---------------------------------------------------------------------------
// Conversion helpers
// ---------------------------------------------------------------------------

fn parse_lang(s: &str) -> gradbot::Lang {
    match s {
        "fr" => gradbot::Lang::Fr,
        "de" => gradbot::Lang::De,
        "es" => gradbot::Lang::Es,
        "pt" => gradbot::Lang::Pt,
        _ => gradbot::Lang::En,
    }
}

fn lang_to_str(lang: gradbot::Lang) -> &'static str {
    match lang {
        gradbot::Lang::En => "en",
        gradbot::Lang::Fr => "fr",
        gradbot::Lang::De => "de",
        gradbot::Lang::Es => "es",
        gradbot::Lang::Pt => "pt",
    }
}

impl SessionConfigWire {
    /// Convert to a `gradbot::SessionConfig`, using defaults for missing fields.
    pub fn to_lib(&self) -> gradbot::SessionConfig {
        gradbot::SessionConfig {
            voice_id: self.voice_id.clone(),
            instructions: self.instructions.clone(),
            language: self
                .language
                .as_deref()
                .map(parse_lang)
                .unwrap_or(gradbot::Lang::En),
            assistant_speaks_first: self.assistant_speaks_first.unwrap_or(true),
            silence_timeout_s: self.silence_timeout_s.unwrap_or(5.0),
            tools: self
                .tools
                .as_ref()
                .map(|t| t.iter().map(|td| td.to_lib()).collect())
                .unwrap_or_default(),
            flush_duration_s: self
                .flush_duration_s
                .unwrap_or(gradbot::DEFAULT_FLUSH_FOR_S),
            padding_bonus: self.padding_bonus.unwrap_or(0.0),
            rewrite_rules: self.rewrite_rules.clone(),
            stt_extra_config: self.stt_extra_config.clone(),
            tts_extra_config: self.tts_extra_config.clone(),
            llm_extra_config: self.llm_extra_config.clone(),
        }
    }
}

impl From<&gradbot::SessionConfig> for SessionConfigWire {
    fn from(c: &gradbot::SessionConfig) -> Self {
        Self {
            voice_id: c.voice_id.clone(),
            instructions: c.instructions.clone(),
            language: Some(lang_to_str(c.language).to_string()),
            assistant_speaks_first: Some(c.assistant_speaks_first),
            silence_timeout_s: Some(c.silence_timeout_s),
            tools: Some(c.tools.iter().map(ToolDefWire::from).collect()),
            flush_duration_s: Some(c.flush_duration_s),
            padding_bonus: Some(c.padding_bonus),
            rewrite_rules: c.rewrite_rules.clone(),
            stt_extra_config: c.stt_extra_config.clone(),
            tts_extra_config: c.tts_extra_config.clone(),
            llm_extra_config: c.llm_extra_config.clone(),
        }
    }
}

impl ToolDefWire {
    fn to_lib(&self) -> gradbot::ToolDef {
        gradbot::ToolDef {
            name: self.name.clone(),
            description: self.description.clone(),
            parameters: self.parameters.clone(),
        }
    }
}

impl From<&gradbot::ToolDef> for ToolDefWire {
    fn from(t: &gradbot::ToolDef) -> Self {
        Self {
            name: t.name.clone(),
            description: t.description.clone(),
            parameters: t.parameters.clone(),
        }
    }
}

// ---------------------------------------------------------------------------
// Config pinning
// ---------------------------------------------------------------------------

/// Merge client config with server-pinned config.
/// Returns the merged `SessionConfig` and the list of field names that were pinned.
pub fn merge_with_pinned(
    client: SessionConfigWire,
    pinned: &SessionConfigWire,
) -> (gradbot::SessionConfig, Vec<String>) {
    let mut pinned_fields = Vec::new();

    macro_rules! pick {
        ($field:ident) => {
            if pinned.$field.is_some() {
                pinned_fields.push(stringify!($field).to_string());
                pinned.$field.clone()
            } else {
                client.$field.clone()
            }
        };
    }

    let merged = SessionConfigWire {
        voice_id: pick!(voice_id),
        instructions: pick!(instructions),
        language: pick!(language),
        assistant_speaks_first: pick!(assistant_speaks_first),
        silence_timeout_s: pick!(silence_timeout_s),
        tools: pick!(tools),
        flush_duration_s: pick!(flush_duration_s),
        padding_bonus: pick!(padding_bonus),
        rewrite_rules: pick!(rewrite_rules),
        stt_extra_config: pick!(stt_extra_config),
        tts_extra_config: pick!(tts_extra_config),
        llm_extra_config: pick!(llm_extra_config),
    };

    (merged.to_lib(), pinned_fields)
}
