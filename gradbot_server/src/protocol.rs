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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_lang_recognizes_all_known_codes() {
        assert_eq!(parse_lang("fr"), gradbot::Lang::Fr);
        assert_eq!(parse_lang("de"), gradbot::Lang::De);
        assert_eq!(parse_lang("es"), gradbot::Lang::Es);
        assert_eq!(parse_lang("pt"), gradbot::Lang::Pt);
        assert_eq!(parse_lang("en"), gradbot::Lang::En);
    }

    #[test]
    fn parse_lang_defaults_unknown_codes_to_english() {
        assert_eq!(parse_lang("xx"), gradbot::Lang::En);
        assert_eq!(parse_lang(""), gradbot::Lang::En);
    }

    #[test]
    fn lang_round_trips_through_str() {
        for lang in [
            gradbot::Lang::En,
            gradbot::Lang::Fr,
            gradbot::Lang::De,
            gradbot::Lang::Es,
            gradbot::Lang::Pt,
        ] {
            assert_eq!(parse_lang(lang_to_str(lang)), lang);
        }
    }

    #[test]
    fn session_config_wire_to_lib_uses_defaults_when_empty() {
        let wire = SessionConfigWire::default();
        let config = wire.to_lib();

        assert_eq!(config.voice_id, None);
        assert_eq!(config.instructions, None);
        assert_eq!(config.language, gradbot::Lang::En);
        assert!(config.assistant_speaks_first);
        assert_eq!(config.silence_timeout_s, 5.0);
        assert!(config.tools.is_empty());
        assert_eq!(config.flush_duration_s, gradbot::DEFAULT_FLUSH_FOR_S);
        assert_eq!(config.padding_bonus, 0.0);
    }

    #[test]
    fn session_config_wire_to_lib_honors_explicit_values() {
        let wire = SessionConfigWire {
            voice_id: Some("voice-1".to_string()),
            instructions: Some("be terse".to_string()),
            language: Some("de".to_string()),
            assistant_speaks_first: Some(false),
            silence_timeout_s: Some(2.5),
            tools: Some(vec![ToolDefWire {
                name: "lookup".to_string(),
                description: "look things up".to_string(),
                parameters: serde_json::json!({"type": "object"}),
            }]),
            flush_duration_s: Some(1.0),
            padding_bonus: Some(-2.0),
            rewrite_rules: Some("de".to_string()),
            stt_extra_config: None,
            tts_extra_config: None,
            llm_extra_config: None,
        };

        let config = wire.to_lib();

        assert_eq!(config.voice_id, Some("voice-1".to_string()));
        assert_eq!(config.language, gradbot::Lang::De);
        assert!(!config.assistant_speaks_first);
        assert_eq!(config.silence_timeout_s, 2.5);
        assert_eq!(config.tools.len(), 1);
        assert_eq!(config.tools[0].name, "lookup");
        assert_eq!(config.flush_duration_s, 1.0);
        assert_eq!(config.padding_bonus, -2.0);
        assert_eq!(config.rewrite_rules, Some("de".to_string()));
    }

    #[test]
    fn session_config_round_trips_through_wire() {
        let original = gradbot::SessionConfig {
            voice_id: Some("voice-2".to_string()),
            instructions: None,
            language: gradbot::Lang::Es,
            assistant_speaks_first: false,
            silence_timeout_s: 7.0,
            tools: vec![gradbot::ToolDef {
                name: "search".to_string(),
                description: "search the web".to_string(),
                parameters: serde_json::json!({}),
            }],
            flush_duration_s: 0.25,
            padding_bonus: 1.5,
            rewrite_rules: None,
            stt_extra_config: None,
            tts_extra_config: None,
            llm_extra_config: None,
        };

        let wire = SessionConfigWire::from(&original);
        let round_tripped = wire.to_lib();

        assert_eq!(round_tripped.voice_id, original.voice_id);
        assert_eq!(round_tripped.language, original.language);
        assert_eq!(
            round_tripped.assistant_speaks_first,
            original.assistant_speaks_first
        );
        assert_eq!(round_tripped.silence_timeout_s, original.silence_timeout_s);
        assert_eq!(round_tripped.tools.len(), 1);
        assert_eq!(round_tripped.tools[0].name, "search");
        assert_eq!(round_tripped.flush_duration_s, original.flush_duration_s);
        assert_eq!(round_tripped.padding_bonus, original.padding_bonus);
    }

    #[test]
    fn merge_with_pinned_overrides_client_fields() {
        let client = SessionConfigWire {
            voice_id: Some("client-voice".to_string()),
            instructions: Some("client instructions".to_string()),
            ..Default::default()
        };
        let pinned = SessionConfigWire {
            voice_id: Some("pinned-voice".to_string()),
            ..Default::default()
        };

        let (merged, pinned_fields) = merge_with_pinned(client, &pinned);

        assert_eq!(merged.voice_id, Some("pinned-voice".to_string()));
        assert_eq!(merged.instructions, Some("client instructions".to_string()));
        assert_eq!(pinned_fields, vec!["voice_id".to_string()]);
    }

    #[test]
    fn merge_with_pinned_falls_back_to_client_when_nothing_pinned() {
        let client = SessionConfigWire {
            voice_id: Some("client-voice".to_string()),
            silence_timeout_s: Some(3.0),
            ..Default::default()
        };
        let pinned = SessionConfigWire::default();

        let (merged, pinned_fields) = merge_with_pinned(client, &pinned);

        assert_eq!(merged.voice_id, Some("client-voice".to_string()));
        assert_eq!(merged.silence_timeout_s, 3.0);
        assert!(pinned_fields.is_empty());
    }

    #[test]
    fn client_message_session_config_deserializes_from_tagged_json() {
        let json = r#"{"type":"session.config","config":{"voice_id":"v1"}}"#;
        let msg: ClientMessage = serde_json::from_str(json).unwrap();
        match msg {
            ClientMessage::SessionConfig { config } => {
                assert_eq!(config.voice_id, Some("v1".to_string()));
            }
            _ => panic!("expected SessionConfig variant"),
        }
    }

    #[test]
    fn client_message_tool_call_result_defaults_is_error_to_false() {
        let json = r#"{"type":"tool_call.result","call_id":"abc","result":"ok"}"#;
        let msg: ClientMessage = serde_json::from_str(json).unwrap();
        match msg {
            ClientMessage::ToolCallResult {
                call_id, is_error, ..
            } => {
                assert_eq!(call_id, "abc");
                assert!(!is_error);
            }
            _ => panic!("expected ToolCallResult variant"),
        }
    }

    #[test]
    fn server_message_error_serializes_with_tag() {
        let msg = ServerMessage::Error {
            message: "boom".to_string(),
        };
        let json = serde_json::to_string(&msg).unwrap();
        assert_eq!(json, r#"{"type":"error","message":"boom"}"#);
    }
}
