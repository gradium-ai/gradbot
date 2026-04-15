//! # gradbot
//!
//! Core library for building voice AI applications with real-time speech-to-text,
//! LLM processing, and text-to-speech.
//!
//! ## Architecture
//!
//! The library implements a multiplexing loop that coordinates:
//! - **Speech-to-Text (STT)**: Converts incoming audio to text transcriptions
//! - **LLM**: Processes transcribed text and generates responses
//! - **Text-to-Speech (TTS)**: Converts LLM responses back to audio
//!
//! ## Quick Start
//!
//! ```ignore
//! use gradbot::{GradbotClients, MsgOut, SessionConfig};
//!
//! // 1. Create clients (uses environment variables for API keys)
//! let clients = GradbotClients::new(None, None, None, None, None, None).await?;
//!
//! // 2. Start a session
//! let (input, mut output) = clients.start_session(Some(config), Format::OggOpus).await?;
//!
//! // 3. Spawn a producer to send audio
//! tokio::spawn(async move {
//!     input.send_audio(audio_bytes).await.ok();
//!     // input is dropped when done -> session ends normally
//! });
//!
//! // 4. Consume output messages
//! while let Some(msg) = output.receive().await? {
//!     match msg {
//!         MsgOut::Audio { data, .. } => { /* send audio */ }
//!         MsgOut::TtsText { text, .. } => { /* send caption */ }
//!         MsgOut::SttText { text, .. } => { /* send transcription */ }
//!         MsgOut::Event { event, .. } => { /* handle event */ }
//!     }
//! }
//! ```
//!
//! ## Environment Variables
//!
//! - `GRADIUM_API_KEY` - API key for Gradium STT/TTS services
//! - `GRADIUM_BASE_URL` - Base URL for Gradium services (optional, defaults to `https://api.gradium.ai`)
//! - `LLM_API_KEY` - API key for OpenAI-compatible LLM API (falls back to `OPENAI_API_KEY`)
//! - `LLM_BASE_URL` - Base URL for LLM API (optional, defaults to OpenAI's API)
//! - `LLM_MODEL` - LLM model name (optional, auto-detected if single model available)
//!
//! ## Channel Semantics
//!
//! - **Normal termination**: When `SessionInputHandle` is dropped (client disconnects),
//!   `output.receive()` returns `Ok(None)`.
//! - **Internal error**: On any processing error, `output.receive()` returns `Err(e)`.
//!
//! ## Message Flow
//!
//! ```text
//! ┌─────────────┐     MsgIn::Audio      ┌─────────────┐
//! │  Producer   │ ──────────────────────▶│             │
//! │   (your     │     MsgIn::Config      │   session   │
//! │    loop)    │ ──────────────────────▶│   future    │
//! └─────────────┘                        │             │
//!                                        │             │
//! ┌─────────────┐     MsgOut::Audio      │             │
//! │  Consumer   │ ◀──────────────────────│             │
//! │   (your     │     MsgOut::TtsText    │             │
//! │    loop)    │ ◀──────────────────────│             │
//! └─────────────┘     MsgOut::SttText    │             │
//!                 ◀──────────────────────│             │
//!                     MsgOut::Event      │             │
//!                 ◀──────────────────────└─────────────┘
//! ```
//!
//! ## Timing
//!
//! All timestamps (`start_s`, `stop_s`, `time_s`) are relative to the start of the
//! session, measured in seconds from when the first audio was received.
//!
//! - `start_s`: When this audio/text segment begins
//! - `stop_s`: When this audio/text segment ends
//! - `time_s`: When this event occurred

pub mod decoder;
pub mod encoder;
mod llm;
#[cfg(test)]
pub mod mock;
mod multiplex;
mod speech_to_text;
mod system_prompt;
pub mod text_to_speech;
pub mod utils;
mod wav;

use anyhow::{Context, Result};
use std::sync::Arc;

// Re-export public API
pub use llm::{Llm, LlmConfig, ToolCall, ToolCallHandle, ToolDef, ToolResult};
pub use multiplex::{
    DEFAULT_FLUSH_FOR_S, Event, MsgIn, MsgOut, OUTPUT_FRAME_SIZE, OUTPUT_SAMPLE_RATE,
    SessionConfig, SessionInputHandle, SessionOutputHandle, start_session,
};
pub use speech_to_text::SttClient;
pub use system_prompt::Lang;
pub use text_to_speech::TtsClient;

/// Audio format pair for input (decoding) and output (encoding).
pub struct IoFormat {
    pub input: crate::decoder::Format,
    pub output: crate::encoder::Format,
}

/// Default Gradium API base URL.
pub const DEFAULT_GRADIUM_BASE_URL: &str = "https://api.gradium.ai/api";

/// Gender of a voice.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Gender {
    Masculine,
    Feminine,
}

impl Gender {
    pub fn as_str(&self) -> &'static str {
        match self {
            Gender::Masculine => "Masculine",
            Gender::Feminine => "Feminine",
        }
    }
}

/// Country/accent of a voice.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Country {
    Us,
    Gb,
    Fr,
    De,
    Mx,
    Es,
    Br,
}

impl Country {
    pub fn as_str(&self) -> &'static str {
        match self {
            Country::Us => "us",
            Country::Gb => "gb",
            Country::Fr => "fr",
            Country::De => "de",
            Country::Mx => "mx",
            Country::Es => "es",
            Country::Br => "br",
        }
    }

    pub fn full_name(&self) -> &'static str {
        match self {
            Country::Us => "United States",
            Country::Gb => "United Kingdom",
            Country::Fr => "France",
            Country::De => "Germany",
            Country::Mx => "Mexico",
            Country::Es => "Spain",
            Country::Br => "Brazil",
        }
    }
}

/// Flagship voice information: name, voice ID, language, country, gender, and description.
#[derive(Debug, Clone, Copy)]
pub struct FlagshipVoice {
    pub name: &'static str,
    pub voice_id: &'static str,
    pub language: Lang,
    pub country: Country,
    pub gender: Gender,
    pub description: &'static str,
}

/// All available flagship voices.
pub const FLAGSHIP_VOICES: &[FlagshipVoice] = &[
    // English (US) voices
    FlagshipVoice {
        name: "Emma",
        voice_id: "YTpq7expH9539ERJ",
        language: Lang::En,
        country: Country::Us,
        gender: Gender::Feminine,
        description: "A warm and welcoming voice with a friendly American accent, perfect for creating a comfortable conversational experience.",
    },
    FlagshipVoice {
        name: "Kent",
        voice_id: "LFZvm12tW_z0xfGo",
        language: Lang::En,
        country: Country::Us,
        gender: Gender::Masculine,
        description: "A confident and professional voice with a clear American accent, ideal for business and informative content.",
    },
    // English (UK) voices
    FlagshipVoice {
        name: "Sydney",
        voice_id: "jtEKaLYNn6iif5PR",
        language: Lang::En,
        country: Country::Gb,
        gender: Gender::Feminine,
        description: "A sophisticated and articulate voice with a refined British accent, suitable for elegant and professional applications.",
    },
    FlagshipVoice {
        name: "John",
        voice_id: "KWJiFWu2O9nMPYcR",
        language: Lang::En,
        country: Country::Gb,
        gender: Gender::Masculine,
        description: "A distinguished and authoritative voice with a classic British accent, perfect for narration and formal content.",
    },
    FlagshipVoice {
        name: "Eva",
        voice_id: "ubuXFxVQwVYnZQhy",
        language: Lang::En,
        country: Country::Gb,
        gender: Gender::Feminine,
        description: "A lively and engaging voice with a British accent, great for dynamic and energetic conversations.",
    },
    FlagshipVoice {
        name: "Jack",
        voice_id: "m86j6D7UZpGzHsNu",
        language: Lang::En,
        country: Country::Gb,
        gender: Gender::Masculine,
        description: "A relaxed and friendly voice with a British accent, ideal for casual and approachable interactions.",
    },
    // French voices
    FlagshipVoice {
        name: "Elise",
        voice_id: "b35yykvVppLXyw_l",
        language: Lang::Fr,
        country: Country::Fr,
        gender: Gender::Feminine,
        description: "A charming and melodic voice with an authentic French accent, perfect for romantic and artistic content.",
    },
    FlagshipVoice {
        name: "Leo",
        voice_id: "axlOaUiFyOZhy4nv",
        language: Lang::Fr,
        country: Country::Fr,
        gender: Gender::Masculine,
        description: "A smooth and charismatic voice with a French accent, ideal for sophisticated and cultured applications.",
    },
    // German voices
    FlagshipVoice {
        name: "Mia",
        voice_id: "-uP9MuGtBqAvEyxI",
        language: Lang::De,
        country: Country::De,
        gender: Gender::Feminine,
        description: "A clear and precise voice with a German accent, excellent for technical and professional content.",
    },
    FlagshipVoice {
        name: "Maximilian",
        voice_id: "0y1VZjPabOBU3rWy",
        language: Lang::De,
        country: Country::De,
        gender: Gender::Masculine,
        description: "A strong and reliable voice with a German accent, perfect for authoritative and instructional content.",
    },
    // Spanish voices
    FlagshipVoice {
        name: "Valentina",
        voice_id: "B36pbz5_UoWn4BDl",
        language: Lang::Es,
        country: Country::Mx,
        gender: Gender::Feminine,
        description: "A vibrant and expressive voice with a Mexican Spanish accent, great for lively and engaging conversations.",
    },
    FlagshipVoice {
        name: "Sergio",
        voice_id: "xu7iJ_fn2ElcWp2s",
        language: Lang::Es,
        country: Country::Es,
        gender: Gender::Masculine,
        description: "A warm and passionate voice with a Castilian Spanish accent, ideal for expressive and emotional content.",
    },
    // Portuguese voices
    FlagshipVoice {
        name: "Alice",
        voice_id: "pYcGZz9VOo4n2ynh",
        language: Lang::Pt,
        country: Country::Br,
        gender: Gender::Feminine,
        description: "A bright and cheerful voice with a Brazilian Portuguese accent, perfect for friendly and upbeat interactions.",
    },
    FlagshipVoice {
        name: "Davi",
        voice_id: "M-FvVo9c-jGR4PgP",
        language: Lang::Pt,
        country: Country::Br,
        gender: Gender::Masculine,
        description: "A calm and reassuring voice with a Brazilian Portuguese accent, ideal for supportive and conversational content.",
    },
];

/// Returns all available flagship voices.
///
/// # Example
///
/// ```
/// use gradbot::flagship_voices;
///
/// for voice in flagship_voices() {
///     println!("{}: {} ({:?})", voice.name, voice.voice_id, voice.language);
/// }
/// ```
pub fn flagship_voices() -> &'static [FlagshipVoice] {
    FLAGSHIP_VOICES
}

/// Lazily initialized lookup table for flagship voices (lowercase name -> index).
static FLAGSHIP_VOICE_LOOKUP: std::sync::OnceLock<std::collections::HashMap<String, usize>> =
    std::sync::OnceLock::new();

fn get_flagship_voice_lookup() -> &'static std::collections::HashMap<String, usize> {
    FLAGSHIP_VOICE_LOOKUP.get_or_init(|| {
        FLAGSHIP_VOICES
            .iter()
            .enumerate()
            .map(|(i, v)| (v.name.to_lowercase(), i))
            .collect()
    })
}

/// Look up a flagship voice by name.
///
/// Returns the voice ID and language for the given voice name.
/// The lookup is case-insensitive.
///
/// # Errors
///
/// Returns an error if the voice name is not a known flagship voice.
///
/// # Example
///
/// ```
/// use gradbot::{flagship_voice, Lang};
///
/// let voice = flagship_voice("emma").unwrap();
/// assert_eq!(voice.voice_id, "YTpq7expH9539ERJ");
/// assert_eq!(voice.language, Lang::En);
/// ```
pub fn flagship_voice(name: &str) -> Result<FlagshipVoice> {
    let name_lower = name.to_lowercase();
    get_flagship_voice_lookup()
        .get(&name_lower)
        .map(|&i| FLAGSHIP_VOICES[i])
        .ok_or_else(|| anyhow::anyhow!("unknown flagship voice: {name}"))
}

/// Shared clients for creating voice AI sessions.
///
/// This struct holds the TTS, STT, and LLM clients and provides a convenient way
/// to start multiple sessions without recreating clients.
///
/// # Example
///
/// ```ignore
/// // Create clients with defaults (reads API keys from environment)
/// let clients = GradbotClients::new(None, None, None, None, None, None).await?;
///
/// // Start a session
/// let io = IoFormat { input: decoder::Format::OggOpus, output: encoder::Format::OggOpus };
/// let (input, mut output) = clients.start_session(Some(config), io).await?;
/// ```
pub struct GradbotClients {
    tts_client: Arc<TtsClient>,
    stt_client: Arc<SttClient>,
    llm: Arc<Llm>,
}

/// Create clients and start a session in one call.
///
/// This is a convenience function that combines client creation and session start.
/// For multiple sessions, use [`GradbotClients`] directly to reuse clients.
///
/// # Arguments
///
/// * `gradium_api_key` - API key for Gradium STT/TTS services. Defaults to `GRADIUM_API_KEY` env var.
/// * `gradium_base_url` - Base URL for Gradium services. Falls back to `GRADIUM_BASE_URL` env var, then `https://api.gradium.ai`.
/// * `llm_base_url` - Base URL for OpenAI-compatible LLM API. Falls back to `LLM_BASE_URL` env var, then OpenAI's API.
/// * `llm_model_name` - LLM model name. Resolution order: this parameter > `LLM_MODEL` env var > auto-detect.
/// * `llm_api_key` - API key for LLM API. Resolution: this parameter > `LLM_API_KEY` env var > `OPENAI_API_KEY` env var.
/// * `max_completion_tokens` - Maximum tokens for LLM responses. Defaults to 4096.
/// * `session_config` - Initial session configuration.
/// * `io_format` - Audio format pair for input decoding and output encoding.
///
/// # Example
///
/// ```ignore
/// let (input, mut output) = gradbot::run(
///     None, None, None, None, None, None,
///     Some(config),
///     IoFormat { input: decoder::Format::pcm(24000), output: encoder::Format::OggOpus },
/// ).await?;
/// ```
#[allow(clippy::too_many_arguments)]
pub async fn run(
    gradium_api_key: Option<&str>,
    gradium_base_url: Option<&str>,
    llm_base_url: Option<&str>,
    llm_model_name: Option<&str>,
    llm_api_key: Option<&str>,
    max_completion_tokens: Option<u32>,
    session_config: Option<SessionConfig>,
    io_format: IoFormat,
) -> Result<(SessionInputHandle, SessionOutputHandle)> {
    let clients = GradbotClients::new(
        gradium_api_key,
        gradium_base_url,
        llm_base_url,
        llm_model_name,
        llm_api_key,
        max_completion_tokens,
    )
    .await?;
    clients.start_session(session_config, io_format).await
}

impl GradbotClients {
    /// Create new clients with optional configuration.
    ///
    /// # Arguments
    ///
    /// * `gradium_api_key` - API key for Gradium STT/TTS services. Defaults to `GRADIUM_API_KEY` env var.
    /// * `gradium_base_url` - Base URL for Gradium services. Falls back to `GRADIUM_BASE_URL` env var, then `https://api.gradium.ai`.
    /// * `llm_base_url` - Base URL for OpenAI-compatible LLM API. Falls back to `LLM_BASE_URL` env var, then OpenAI's API.
    /// * `llm_model_name` - LLM model name. Resolution order: this parameter > `LLM_MODEL` env var > auto-detect (uses single available model).
    /// * `llm_api_key` - API key for LLM API. Resolution: this parameter > `LLM_API_KEY` env var > `OPENAI_API_KEY` env var.
    /// * `max_completion_tokens` - Maximum tokens for LLM responses. Defaults to 4096.
    pub async fn new(
        gradium_api_key: Option<&str>,
        gradium_base_url: Option<&str>,
        llm_base_url: Option<&str>,
        llm_model_name: Option<&str>,
        llm_api_key: Option<&str>,
        max_completion_tokens: Option<u32>,
    ) -> Result<Self> {
        let gradium_base_url = gradium_base_url
            .map(|s| s.to_string())
            .or_else(|| std::env::var("GRADIUM_BASE_URL").ok())
            .unwrap_or_else(|| DEFAULT_GRADIUM_BASE_URL.to_string());
        let llm_base_url = llm_base_url.map(|s| s.to_string());
        let max_completion_tokens = max_completion_tokens.unwrap_or(4096);

        tracing::info!(
            gradium_base_url,
            llm_base_url = llm_base_url.as_deref().unwrap_or("(default)"),
            "creating clients"
        );

        let tts_client = Arc::new(TtsClient::new(gradium_api_key, &gradium_base_url).context(
            format!("TTS: failed to create client (base_url={gradium_base_url})"),
        )?);
        let stt_client = Arc::new(SttClient::new(gradium_api_key, &gradium_base_url).context(
            format!("STT: failed to create client (base_url={gradium_base_url})"),
        )?);
        let llm_base_url_display = llm_base_url
            .as_deref()
            .unwrap_or("https://api.openai.com/v1")
            .to_string();
        let llm = Arc::new(
            Llm::new(
                llm_base_url,
                max_completion_tokens,
                llm_model_name.map(|s| s.to_string()),
                llm_api_key.map(|s| s.to_string()),
            )
            .await
            .context(format!(
                "LLM: failed to create client (base_url={llm_base_url_display})"
            ))?,
        );

        Ok(Self {
            tts_client,
            stt_client,
            llm,
        })
    }

    /// Get a reference to the TTS client for direct text-to-speech synthesis.
    pub fn tts_client(&self) -> &TtsClient {
        &self.tts_client
    }

    /// Start a new voice AI session.
    ///
    /// Returns separate handles for input (sending audio/config) and output (receiving messages).
    ///
    /// # Arguments
    ///
    /// * `initial_config` - Optional session configuration (voice, language, instructions)
    /// * `io_format` - Audio format pair for input decoding and output encoding
    pub async fn start_session(
        &self,
        initial_config: Option<SessionConfig>,
        io_format: IoFormat,
    ) -> Result<(SessionInputHandle, SessionOutputHandle)> {
        start_session(
            self.tts_client.clone(),
            self.stt_client.clone(),
            self.llm.clone(),
            initial_config,
            io_format,
        )
        .await
    }
}
