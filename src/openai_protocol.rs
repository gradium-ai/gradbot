//! OpenAI Realtime API protocol types
//!
//! See OpenAI's docs:
//! - <https://platform.openai.com/docs/api-reference/realtime>
//! - <https://platform.openai.com/docs/api-reference/realtime-client-events>
//! - <https://platform.openai.com/docs/api-reference/realtime-server-events>

use base64_serde::base64_serde_type;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashMap;

base64_serde_type!(Base64Standard, base64::engine::general_purpose::STANDARD);

/// Generate a random ID with a prefix (e.g., "event_BJhGUIswO2u7vA2Cxw3Jy")
pub fn random_id(prefix: &str) -> String {
    use rand::Rng;
    const N_CHARACTERS: usize = 21;
    const ALPHABET: &[u8] = b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";

    let mut rng = rand::thread_rng();
    let random_part: String = (0..N_CHARACTERS)
        .map(|_| {
            let idx = rng.gen_range(0..ALPHABET.len());
            ALPHABET[idx] as char
        })
        .collect();

    format!("{}_{}", prefix, random_part)
}

/// Error details for error events
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ErrorDetails {
    #[serde(rename = "type")]
    pub error_type: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub code: Option<String>,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub param: Option<String>,
    /// Ours, not part of the OpenAI API
    #[serde(skip_serializing_if = "Option::is_none")]
    pub details: Option<Value>,
}

/// Session configuration
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionConfig {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub instructions: Option<String>,
    #[serde(skip)]
    pub voice: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub voice_id: Option<String>,
    pub allow_recording: bool,
    #[serde(rename = "gradium.lang")]
    pub lang: Option<String>,
}

/// Response object
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Response {
    pub object: String, // Always "realtime.response"
    /// We currently only use in_progress
    pub status: ResponseStatus,
    pub voice: String,
    #[serde(default)]
    pub chat_history: Vec<HashMap<String, Value>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ResponseStatus {
    InProgress,
    Completed,
    Cancelled,
    Failed,
    Incomplete,
}

/// Transcript logprob (not currently used but defined in the protocol)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TranscriptLogprob {
    pub bytes: Vec<u8>,
    pub logprob: f64,
    pub token: String,
}

/// Server events (from OpenAI to client)
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum ServerEvent {
    #[serde(rename = "error")]
    Error {
        event_id: String,
        error: ErrorDetails,
    },

    #[serde(rename = "session.updated")]
    SessionUpdated {
        event_id: String,
        session: SessionConfig,
    },

    #[serde(rename = "response.created")]
    ResponseCreated {
        event_id: String,
        response: Response,
    },

    #[serde(rename = "response.text.delta")]
    ResponseTextDelta { event_id: String, delta: String },

    #[serde(rename = "response.text.done")]
    ResponseTextDone { event_id: String, text: String },

    #[serde(rename = "response.audio.delta")]
    ResponseAudioDelta {
        event_id: String,
        /// Opus audio data (base64 encoded/decoded automatically)
        #[serde(with = "Base64Standard")]
        delta: Vec<u8>,
    },

    #[serde(rename = "response.audio.done")]
    ResponseAudioDone { event_id: String },

    #[serde(rename = "conversation.item.input_audio_transcription.delta")]
    ConversationItemInputAudioTranscriptionDelta {
        event_id: String,
        delta: String,
        /// Unmute extension
        start_time: f64,
    },

    #[serde(rename = "input_audio_buffer.speech_started")]
    InputAudioBufferSpeechStarted { event_id: String },

    #[serde(rename = "input_audio_buffer.speech_stopped")]
    InputAudioBufferSpeechStopped { event_id: String },

    #[serde(rename = "unmute.additional_outputs")]
    UnmuteAdditionalOutputs { event_id: String, args: Value },

    #[serde(rename = "unmute.response.text.delta.ready")]
    UnmuteResponseTextDeltaReady { event_id: String, delta: String },

    #[serde(rename = "unmute.response.audio.delta.ready")]
    UnmuteResponseAudioDeltaReady {
        event_id: String,
        number_of_samples: i32,
    },

    #[serde(rename = "unmute.response.function_call")]
    UnmuteResponseFunctionCall {
        event_id: String,
        call_id: String,
        name: String,
        arguments: Value,
    },

    #[serde(rename = "unmute.interrupted_by_vad")]
    UnmuteInterruptedByVAD { event_id: String },
}

impl ServerEvent {
    /// Get the event ID for any server event
    pub fn event_id(&self) -> &str {
        match self {
            ServerEvent::Error { event_id, .. }
            | ServerEvent::SessionUpdated { event_id, .. }
            | ServerEvent::ResponseCreated { event_id, .. }
            | ServerEvent::ResponseTextDelta { event_id, .. }
            | ServerEvent::ResponseTextDone { event_id, .. }
            | ServerEvent::ResponseAudioDelta { event_id, .. }
            | ServerEvent::ResponseAudioDone { event_id, .. }
            | ServerEvent::ConversationItemInputAudioTranscriptionDelta { event_id, .. }
            | ServerEvent::InputAudioBufferSpeechStarted { event_id, .. }
            | ServerEvent::InputAudioBufferSpeechStopped { event_id, .. }
            | ServerEvent::UnmuteAdditionalOutputs { event_id, .. }
            | ServerEvent::UnmuteResponseTextDeltaReady { event_id, .. }
            | ServerEvent::UnmuteResponseAudioDeltaReady { event_id, .. }
            | ServerEvent::UnmuteResponseFunctionCall { event_id, .. }
            | ServerEvent::UnmuteInterruptedByVAD { event_id, .. } => event_id,
        }
    }

    /// Create a new error event
    pub fn error(error: ErrorDetails) -> Self {
        ServerEvent::Error {
            event_id: random_id("event"),
            error,
        }
    }

    /// Create a new session updated event
    pub fn session_updated(session: SessionConfig) -> Self {
        ServerEvent::SessionUpdated {
            event_id: random_id("event"),
            session,
        }
    }

    /// Create a new response created event
    pub fn response_created(response: Response) -> Self {
        ServerEvent::ResponseCreated {
            event_id: random_id("event"),
            response,
        }
    }

    /// Create a new response text delta event
    pub fn response_text_delta(delta: String) -> Self {
        ServerEvent::ResponseTextDelta {
            event_id: random_id("event"),
            delta,
        }
    }

    /// Create a new response text done event
    pub fn response_text_done(text: String) -> Self {
        ServerEvent::ResponseTextDone {
            event_id: random_id("event"),
            text,
        }
    }

    /// Create a new response audio delta event
    pub fn response_audio_delta(delta: Vec<u8>) -> Self {
        ServerEvent::ResponseAudioDelta {
            event_id: random_id("event"),
            delta,
        }
    }

    /// Create a new response audio done event
    pub fn response_audio_done() -> Self {
        ServerEvent::ResponseAudioDone {
            event_id: random_id("event"),
        }
    }

    pub fn conversation_item_input_audio_transcription_delta(
        delta: String,
        start_time: f64,
    ) -> Self {
        ServerEvent::ConversationItemInputAudioTranscriptionDelta {
            event_id: random_id("event"),
            delta,
            start_time,
        }
    }

    pub fn unmute_response_function_call(call_id: String, name: String, arguments: Value) -> Self {
        ServerEvent::UnmuteResponseFunctionCall {
            event_id: random_id("event"),
            call_id,
            name,
            arguments,
        }
    }
}

/// Client events (from client to OpenAI)
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum ClientEvent {
    #[serde(rename = "session.update")]
    SessionUpdate {
        event_id: Option<String>,
        session: SessionConfig,
    },

    #[serde(rename = "input_audio_buffer.append")]
    InputAudioBufferAppend {
        event_id: Option<String>,
        /// Opus audio data (base64 encoded/decoded automatically)
        #[serde(with = "Base64Standard")]
        audio: Vec<u8>,
    },

    /// Used internally for recording, we're not expecting the user to send this
    #[serde(rename = "unmute.input_audio_buffer.append_anonymized")]
    UnmuteInputAudioBufferAppendAnonymized {
        event_id: Option<String>,
        number_of_samples: i32,
    },

    /// Function call result from the client
    #[serde(rename = "unmute.function_call_result")]
    UnmuteFunctionCallResult {
        event_id: Option<String>,
        call_id: String,
        /// JSON result or error message
        result: Value,
        /// If true, result contains an error message
        #[serde(default)]
        is_error: bool,
    },
}

impl ClientEvent {
    /// Create a new session update event
    pub fn session_update(session: SessionConfig) -> Self {
        ClientEvent::SessionUpdate {
            event_id: None,
            session,
        }
    }

    /// Create a new input audio buffer append event
    pub fn input_audio_buffer_append(audio: Vec<u8>) -> Self {
        ClientEvent::InputAudioBufferAppend {
            event_id: None,
            audio,
        }
    }
}

/// Combined event type (can be either client or server)
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Event {
    Server(ServerEvent),
    Client(ClientEvent),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_random_id() {
        let id = random_id("event");
        assert!(id.starts_with("event_"));
        assert_eq!(id.len(), "event_".len() + 21);
    }

    #[test]
    fn test_serialize_error_event() {
        let error = ErrorDetails {
            error_type: "invalid_request".to_string(),
            code: Some("400".to_string()),
            message: "Invalid request".to_string(),
            param: None,
            details: None,
        };

        let event = ServerEvent::error(error);
        let json = serde_json::to_string(&event).unwrap();
        assert!(json.contains(r#""type":"error""#));
        assert!(json.contains("event_"));
    }

    #[test]
    fn test_deserialize_session_update() {
        let json = r#"{
            "type": "session.update",
            "event_id": "event_123",
            "session": {
                "allow_recording": true
            }
        }"#;

        let event: ClientEvent = serde_json::from_str(json).unwrap();
        match event {
            ClientEvent::SessionUpdate { session, .. } => {
                assert!(session.allow_recording);
            }
            _ => panic!("Expected SessionUpdate"),
        }
    }

    #[test]
    fn test_serialize_response_text_delta() {
        let event = ServerEvent::response_text_delta("Hello".to_string());
        let json = serde_json::to_string(&event).unwrap();
        assert!(json.contains(r#""type":"response.text.delta""#));
        assert!(json.contains(r#""delta":"Hello""#));
    }
}
