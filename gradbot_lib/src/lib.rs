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
//! - `GRADIUM_BASE_URL` - Base URL for Gradium services (optional, defaults to `https://api.gradium.ai/api`)
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
    Ie,
    Fr,
    Ca,
    De,
    At,
    Mx,
    Es,
    Br,
    Pt,
}

impl Country {
    pub fn as_str(&self) -> &'static str {
        match self {
            Country::Us => "us",
            Country::Gb => "gb",
            Country::Ie => "ie",
            Country::Fr => "fr",
            Country::Ca => "ca",
            Country::De => "de",
            Country::At => "at",
            Country::Mx => "mx",
            Country::Es => "es",
            Country::Br => "br",
            Country::Pt => "pt",
        }
    }

    pub fn full_name(&self) -> &'static str {
        match self {
            Country::Us => "United States",
            Country::Gb => "United Kingdom",
            Country::Ie => "Ireland",
            Country::Fr => "France",
            Country::Ca => "Canada",
            Country::De => "Germany",
            Country::At => "Austria",
            Country::Mx => "Mexico",
            Country::Es => "Spain",
            Country::Br => "Brazil",
            Country::Pt => "Portugal",
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
        name: "Zoey",
        voice_id: "NbpkqMVS3CJeq2j8",
        language: Lang::En,
        country: Country::Us,
        gender: Gender::Feminine,
        description: "Playful, upbeat and Gen Z energy voice with a standard American accent. Perfect for engaging conversations!",
    },
    FlagshipVoice {
        name: "Sunnie",
        voice_id: "YVzbrdWnnu9FgRn5",
        language: Lang::En,
        country: Country::Us,
        gender: Gender::Feminine,
        description: "A joyful young American voice at medium-high pitch, warm and bubbly, keeping every support call bright and light.",
    },
    FlagshipVoice {
        name: "Marlowe",
        voice_id: "Bla6SbVMczYnOhfK",
        language: Lang::En,
        country: Country::Us,
        gender: Gender::Feminine,
        description: "A bubbly, warm young American voice full of girly charm and a laugh under every word, ideal for friendly support and assistant chats.",
    },
    FlagshipVoice {
        name: "Harper",
        voice_id: "4SZHfMpw-p46Ywgs",
        language: Lang::En,
        country: Country::Us,
        gender: Gender::Feminine,
        description: "Modern, confident and friendly voice with a standard American accent.",
    },
    FlagshipVoice {
        name: "Brooklyn",
        voice_id: "D6COLz20Hw7uh3UK",
        language: Lang::En,
        country: Country::Us,
        gender: Gender::Feminine,
        description: "A warm, effusive young American voice with girly charm and contagious laughter, greeting customers and making everyone feel at home.",
    },
    FlagshipVoice {
        name: "Sterling",
        voice_id: "6MFfc37kq0sBjBjy",
        language: Lang::En,
        country: Country::Us,
        gender: Gender::Masculine,
        description: "A warm energetic American adult voice with theatrical flair that makes every sentence feel like the start of something big.",
    },
    FlagshipVoice {
        name: "Russell",
        voice_id: "_6Aslh2DxfmnRLmP",
        language: Lang::En,
        country: Country::Us,
        gender: Gender::Masculine,
        description: "A high-energy American adult voice that pushes and encourages with the intensity of someone who genuinely believes in you.",
    },
    FlagshipVoice {
        name: "Marcus",
        voice_id: "r2sIQdqqoqgRJuXw",
        language: Lang::En,
        country: Country::Us,
        gender: Gender::Masculine,
        description: "A high-energy resonant American adult voice that speaks with the unshakeable conviction of someone who's already sold you.",
    },
    FlagshipVoice {
        name: "Garrett",
        voice_id: "POBHtemksfWQbng0",
        language: Lang::En,
        country: Country::Us,
        gender: Gender::Masculine,
        description: "A smooth low-pitched American adult voice with the easy confidence and quiet magnetism of someone who never has to raise his voice.",
    },
    FlagshipVoice {
        name: "Damon",
        voice_id: "KUpE0JVhjiIzp1Fk",
        language: Lang::En,
        country: Country::Us,
        gender: Gender::Masculine,
        description: "A bright American adult voice that lights up with the unfiltered excitement of someone explaining their favorite obsession.",
    },
    // English (UK) voices
    FlagshipVoice {
        name: "Tilly",
        voice_id: "4rdlkbxRv4m3UQTW",
        language: Lang::En,
        country: Country::Gb,
        gender: Gender::Feminine,
        description: "A bright, welcoming British English adult voice with the warmth of a great receptionist. Perfect for greeting customers and friendly front-line support.",
    },
    FlagshipVoice {
        name: "Maeve",
        voice_id: "6PWnV0Nq4wu7RVBT",
        language: Lang::En,
        country: Country::Gb,
        gender: Gender::Feminine,
        description: "A sparky, attentive British English adult voice that gets to the point with a smile. Ideal for fast, efficient customer support and helpful voice assistants.",
    },
    FlagshipVoice {
        name: "Freya",
        voice_id: "GgfEkEJtxZR7gnpy",
        language: Lang::En,
        country: Country::Gb,
        gender: Gender::Feminine,
        description: "A young British voice, glossy and confident with fast, girly chatter and bubbly sparkle, perfect for a friendly receptionist or assistant.",
    },
    FlagshipVoice {
        name: "Elodie-Rose",
        voice_id: "y9bQqIDnyxwqK01t",
        language: Lang::En,
        country: Country::Gb,
        gender: Gender::Feminine,
        description: "A young British voice with sharp, enthusiastic energy and crisp articulation, sparky and to the point for customer support and care.",
    },
    FlagshipVoice {
        name: "Toby",
        voice_id: "dME3IWyZBvmh1n1q",
        language: Lang::En,
        country: Country::Gb,
        gender: Gender::Masculine,
        description: "A sparky, attentive British English adult voice that gets to the point with a smile. Ideal for fast, efficient customer support and helpful voice assistants.",
    },
    FlagshipVoice {
        name: "Reuben",
        voice_id: "CF0NgaMwHMMrHZn0",
        language: Lang::En,
        country: Country::Gb,
        gender: Gender::Masculine,
        description: "A confident, upbeat British English adult voice with infectious energy. Perfect for proactive assistants and motivating customer success calls.",
    },
    FlagshipVoice {
        name: "Freddie",
        voice_id: "s_k3kLBbgeK9-xUg",
        language: Lang::En,
        country: Country::Gb,
        gender: Gender::Masculine,
        description: "An easy-going, friendly British English adult voice that puts callers instantly at ease. Great for onboarding assistants and warm concierge interactions.",
    },
    FlagshipVoice {
        name: "Archie",
        voice_id: "kfzLbcdE_yXgLeUI",
        language: Lang::En,
        country: Country::Gb,
        gender: Gender::Masculine,
        description: "A bright, welcoming British English adult voice with the warmth of a great receptionist. Perfect for greeting customers and friendly front-line support.",
    },
    // English (Ireland) voices
    FlagshipVoice {
        name: "Saoirse",
        voice_id: "gqn4ytOULe-TQfjl",
        language: Lang::En,
        country: Country::Ie,
        gender: Gender::Feminine,
        description: "Warm and natural Irish English voice, perfect for conversational applications. Delivers with excellent clarity and genuine emotional expressiveness.",
    },
    FlagshipVoice {
        name: "Aoife",
        voice_id: "vimnD4UQG_36P43U",
        language: Lang::En,
        country: Country::Ie,
        gender: Gender::Feminine,
        description: "Bright and engaging Irish English voice with lively, natural pacing. Ideal for interactive voice assistants and friendly customer service applications.",
    },
    FlagshipVoice {
        name: "Declan",
        voice_id: "I7GYfpcKbafFrYUv",
        language: Lang::En,
        country: Country::Ie,
        gender: Gender::Masculine,
        description: "Calm and confident Irish English voice with steady, natural pacing. Ideal for reassuring customer support applications.",
    },
    FlagshipVoice {
        name: "Cormac",
        voice_id: "JuMRs5W5S52hzuge",
        language: Lang::En,
        country: Country::Ie,
        gender: Gender::Masculine,
        description: "Bright and warm Irish English voice with cheerful, natural pacing. Ideal for welcoming customer service applications.",
    },
    // French (France) voices
    FlagshipVoice {
        name: "Solène",
        voice_id: "YhIHaAfQ0cQPDV9R",
        language: Lang::Fr,
        country: Country::Fr,
        gender: Gender::Feminine,
        description: "A young French voice, bright and high-pitched, warm and enthusiastic at a lively pace, a receptionist who greets everyone with delight.",
    },
    FlagshipVoice {
        name: "Noémie",
        voice_id: "FXxJ9mANRq6BCTX5",
        language: Lang::Fr,
        country: Country::Fr,
        gender: Gender::Feminine,
        description: "A young Parisian voice, warm and expressive with a quick playful pace and delicate high notes, a care specialist who stays bright and witty.",
    },
    FlagshipVoice {
        name: "Maëlys",
        voice_id: "s048cR1l2Jmu4k3B",
        language: Lang::Fr,
        country: Country::Fr,
        gender: Gender::Feminine,
        description: "A young French voice, energetic and enthusiastic with a welcoming, upbeat delivery, sparky and to the point for responsive support.",
    },
    FlagshipVoice {
        name: "Coralie",
        voice_id: "ZeSg853xFACESHHI",
        language: Lang::Fr,
        country: Country::Fr,
        gender: Gender::Feminine,
        description: "A young Parisian voice, sweet and airy with fast, giggly girly charm, a bubbly assistant who makes every interaction feel light.",
    },
    FlagshipVoice {
        name: "Apolline",
        voice_id: "6oIkS98REoVZ1dEw",
        language: Lang::Fr,
        country: Country::Fr,
        gender: Gender::Feminine,
        description: "A sparky, attentive French adult voice that gets to the point with a smile. Ideal for fast, efficient customer support and helpful voice assistants.",
    },
    FlagshipVoice {
        name: "Marius",
        voice_id: "biuhvu17TxVKOcyy",
        language: Lang::Fr,
        country: Country::Fr,
        gender: Gender::Masculine,
        description: "An energetic, well-poised French adult voice with the confident assurance of a natural closer. Ideal for sales pitches and high-conviction content.",
    },
    FlagshipVoice {
        name: "Jules",
        voice_id: "YKeBw3OV1RgpdhLh",
        language: Lang::Fr,
        country: Country::Fr,
        gender: Gender::Masculine,
        description: "A lively, expressive French adult voice that lights up around topics it loves. Great for animated recommendations and high-energy dialogue.",
    },
    FlagshipVoice {
        name: "Gaspard",
        voice_id: "iEu63s1rhn_kegTr",
        language: Lang::Fr,
        country: Country::Fr,
        gender: Gender::Masculine,
        description: "A warm, grounded French adult voice with the easy confidence of a trusted friend. Ideal for friendly assistants and peer-to-peer dialogue.",
    },
    FlagshipVoice {
        name: "Damien",
        voice_id: "25AzBFyp6svYnJsj",
        language: Lang::Fr,
        country: Country::Fr,
        gender: Gender::Masculine,
        description: "An intense, engaging French adult voice that pushes with real conviction. Built for coaching and motivational content.",
    },
    FlagshipVoice {
        name: "Augustin",
        voice_id: "Tek4tJXiX6_yvXq7",
        language: Lang::Fr,
        country: Country::Fr,
        gender: Gender::Masculine,
        description: "A curious, animated French adult voice that lights up when sharing an unexpected fact. Perfect for geeky explainers and trivia-driven dialogue.",
    },
    // French (Canada) voices
    FlagshipVoice {
        name: "Mélanie",
        voice_id: "xynYWquoAsrvM7UY",
        language: Lang::Fr,
        country: Country::Ca,
        gender: Gender::Feminine,
        description: "A warm, welcoming Québécois French (FR-CA) adult voice with natural local charm. Perfect for greeting customers and friendly front-line support.",
    },
    FlagshipVoice {
        name: "Maude",
        voice_id: "sBLwTd5womVX8JOw",
        language: Lang::Fr,
        country: Country::Ca,
        gender: Gender::Feminine,
        description: "A bubbly, reassuring Québécois French (FR-CA) adult voice that puts callers instantly at ease. Great for onboarding assistants and step-by-step guidance.",
    },
    // German (Germany) voices
    FlagshipVoice {
        name: "Resi",
        voice_id: "MAYVpVTYBzLRqNC7",
        language: Lang::De,
        country: Country::De,
        gender: Gender::Feminine,
        description: "Warm, grounded Bavarian woman's voice with a measured, trustworthy delivery. At home in advisory, negotiation, and folksy storytelling, like the wise neighbor who always has a fair proverb ready.",
    },
    FlagshipVoice {
        name: "Lorena",
        voice_id: "aBNlTApBeOlVKa23",
        language: Lang::De,
        country: Country::De,
        gender: Gender::Feminine,
        description: "A warm young German voice, gentle and lively like a kind big sister, greeting customers with genuine warmth on the support line.",
    },
    FlagshipVoice {
        name: "Jette",
        voice_id: "4Mn9VfG2wsLLEzi5",
        language: Lang::De,
        country: Country::De,
        gender: Gender::Feminine,
        description: "An upbeat young German voice with a light girlish tone and warm bounce, a chipper assistant who keeps things moving with a smile.",
    },
    FlagshipVoice {
        name: "Femke",
        voice_id: "W4IqRNmU0pbxrKyn",
        language: Lang::De,
        country: Country::De,
        gender: Gender::Feminine,
        description: "A joyful young German voice at medium-high pitch, warm and bubbly, a bright, friendly receptionist who puts every caller at ease.",
    },
    FlagshipVoice {
        name: "Annika",
        voice_id: "p6Uutkyi3j2iNAUu",
        language: Lang::De,
        country: Country::De,
        gender: Gender::Feminine,
        description: "A sparky, attentive German adult voice that gets to the point with a smile. Ideal for fast, efficient customer support and helpful voice assistants.",
    },
    FlagshipVoice {
        name: "Wastl",
        voice_id: "2cx941cbFIXRc0ok",
        language: Lang::De,
        country: Country::De,
        gender: Gender::Masculine,
        description: "Lively, charming Bavarian man's voice with quick comedic timing and a playful, self-deprecating wink. Perfect for upbeat conversational content and good-news / bad-news moments.",
    },
    FlagshipVoice {
        name: "Mats",
        voice_id: "Kf5m22mROozoMWj3",
        language: Lang::De,
        country: Country::De,
        gender: Gender::Masculine,
        description: "A sparky, attentive German adult voice that gets to the point with a smile. Ideal for fast, efficient customer support and helpful voice assistants.",
    },
    FlagshipVoice {
        name: "Leon",
        voice_id: "20zdyYrQPzKlCwkk",
        language: Lang::De,
        country: Country::De,
        gender: Gender::Masculine,
        description: "A bright, welcoming German adult voice with the warmth of a great receptionist. Perfect for greeting customers and friendly front-line support.",
    },
    FlagshipVoice {
        name: "Henrik",
        voice_id: "yyS1KYWs6mXoEw7D",
        language: Lang::De,
        country: Country::De,
        gender: Gender::Masculine,
        description: "An easy-going, friendly German adult voice that puts callers instantly at ease. Great for onboarding assistants and warm concierge interactions.",
    },
    FlagshipVoice {
        name: "Erik",
        voice_id: "lbpBQTVCOcOHJ5zS",
        language: Lang::De,
        country: Country::De,
        gender: Gender::Masculine,
        description: "A confident, upbeat German adult voice with infectious energy. Perfect for proactive assistants and motivating customer success calls.",
    },
    FlagshipVoice {
        name: "Anton",
        voice_id: "3ZKKapPOvuWFcw9f",
        language: Lang::De,
        country: Country::De,
        gender: Gender::Masculine,
        description: "A vibrant, engaging German adult voice that makes every explanation feel personal. Built for product guides and walk-through assistants.",
    },
    // German (Austria) voices
    FlagshipVoice {
        name: "Sophie",
        voice_id: "TXbEUrHXNFlYBBKb",
        language: Lang::De,
        country: Country::At,
        gender: Gender::Feminine,
        description: "A warm, easy-going Austrian German adult voice with effortless local charm. Perfect for friendly assistants and welcoming customer support.",
    },
    // Spanish (Spain) voices
    FlagshipVoice {
        name: "Vera",
        voice_id: "iTQW2xFICXk8riV4",
        language: Lang::Es,
        country: Country::Es,
        gender: Gender::Feminine,
        description: "A sweet, expressive young Castilian Spanish voice, soft and girly with excited emphasis, a warm, welcoming customer care specialist.",
    },
    FlagshipVoice {
        name: "Noa",
        voice_id: "b6FvJAiokjdqIti4",
        language: Lang::Es,
        country: Country::Es,
        gender: Gender::Feminine,
        description: "A glossy, confident young Castilian Spanish voice with fast, girly charm and warmth, a bubbly assistant that makes every chat sparkle.",
    },
    FlagshipVoice {
        name: "Lucía-Sol",
        voice_id: "3p0eIGbkmny71GlA",
        language: Lang::Es,
        country: Country::Es,
        gender: Gender::Feminine,
        description: "A bouncy, chipper young Castilian Spanish voice with a cute upward lilt, bringing playful energy to friendly reception and support.",
    },
    FlagshipVoice {
        name: "Candela",
        voice_id: "c9wqBDmQiBie5q6Y",
        language: Lang::Es,
        country: Country::Es,
        gender: Gender::Feminine,
        description: "An enthusiastic, bubbly young Castilian Spanish voice, animated like a beauty guru mid-tutorial, adding fun energy to customer care.",
    },
    FlagshipVoice {
        name: "Alba",
        voice_id: "u2UscyAnHilpiwmf",
        language: Lang::Es,
        country: Country::Es,
        gender: Gender::Feminine,
        description: "A sweet, expressive young Castilian Spanish voice, soft and girly with lively emphasis, sparky and to the point for efficient support.",
    },
    FlagshipVoice {
        name: "Mateo",
        voice_id: "sVLgzKMqaptUdaY8",
        language: Lang::Es,
        country: Country::Es,
        gender: Gender::Masculine,
        description: "A sparky, attentive Peninsular Spanish adult voice that gets to the point with a smile. Ideal for fast, efficient customer support and helpful voice assistants.",
    },
    FlagshipVoice {
        name: "Marcos",
        voice_id: "jvPx8j8zLGQ3utZz",
        language: Lang::Es,
        country: Country::Es,
        gender: Gender::Masculine,
        description: "A confident, upbeat Peninsular Spanish adult voice with infectious energy. Perfect for proactive assistants and motivating customer success calls.",
    },
    FlagshipVoice {
        name: "Iker",
        voice_id: "t-_TS1e-0GzDAX02",
        language: Lang::Es,
        country: Country::Es,
        gender: Gender::Masculine,
        description: "An easy-going, friendly Peninsular Spanish adult voice that puts callers instantly at ease. Great for onboarding assistants and warm concierge interactions.",
    },
    FlagshipVoice {
        name: "Alvaro",
        voice_id: "ZeL1KGaZ4BZ2w0Np",
        language: Lang::Es,
        country: Country::Es,
        gender: Gender::Masculine,
        description: "A bright, welcoming Peninsular Spanish adult voice with the warmth of a great receptionist. Perfect for greeting customers and friendly front-line support.",
    },
    // Spanish (Mexico) voices
    FlagshipVoice {
        name: "Ximena",
        voice_id: "VDwnGxAo68C8U8vC",
        language: Lang::Es,
        country: Country::Mx,
        gender: Gender::Feminine,
        description: "A playful, expressive Mexican Spanish adult voice that turns everyday moments into stories. Great for engaging assistants and personable conversational AI.",
    },
    FlagshipVoice {
        name: "Valentina",
        voice_id: "B36pbz5_UoWn4BDl",
        language: Lang::Es,
        country: Country::Mx,
        gender: Gender::Feminine,
        description: "A warm and engaging Mexican female voice perfect for natural storytelling and connecting like a genuine friend.",
    },
    FlagshipVoice {
        name: "Emiliano",
        voice_id: "tWll9uiMafMXfOGw",
        language: Lang::Es,
        country: Country::Mx,
        gender: Gender::Masculine,
        description: "An assured, upbeat Mexican Spanish adult voice with a touch of humor. Built for confident product guides and proactive customer success.",
    },
    FlagshipVoice {
        name: "Diego",
        voice_id: "n7vovxcDTVG4gClo",
        language: Lang::Es,
        country: Country::Mx,
        gender: Gender::Masculine,
        description: "A lively, expressive Mexican Spanish adult voice with natural storytelling flair. Great for animated assistants and engaging conversational AI.",
    },
    // Portuguese (Brazil) voices
    FlagshipVoice {
        name: "Rafaela",
        voice_id: "6k6cRt7QJfm5ChrH",
        language: Lang::Pt,
        country: Country::Br,
        gender: Gender::Feminine,
        description: "A spirited young voice with bell-like clarity and joyful bounce, narrating like the best sleepover ever, a light, fun personal assistant.",
    },
    FlagshipVoice {
        name: "Manuela-Lu",
        voice_id: "KgC2Nqnjj48NUiyV",
        language: Lang::Pt,
        country: Country::Br,
        gender: Gender::Feminine,
        description: "A bright, sweet, chirpy young voice with breathless excitement and a constant smile, a warm receptionist who greets everyone like a friend.",
    },
    FlagshipVoice {
        name: "Isadora",
        voice_id: "CtNvKUUMrx1h56ih",
        language: Lang::Pt,
        country: Country::Br,
        gender: Gender::Feminine,
        description: "An upbeat young voice with a light girlish tone and warm bounce, quick and to the point, ideal for efficient, caring customer service.",
    },
    FlagshipVoice {
        name: "Bianca",
        voice_id: "uCqxlQCKi8sPHwG2",
        language: Lang::Pt,
        country: Country::Br,
        gender: Gender::Feminine,
        description: "A bright, welcoming Brazilian Portuguese adult voice with the warmth of a great receptionist. Perfect for greeting customers and friendly front-line support.",
    },
    FlagshipVoice {
        name: "Beatriz",
        voice_id: "E8Zwjozrxupd4iQD",
        language: Lang::Pt,
        country: Country::Br,
        gender: Gender::Feminine,
        description: "A glossy, confident young voice with charismatic warmth and sparkle, chatting quickly, a bubbly support agent who makes callers feel special.",
    },
    FlagshipVoice {
        name: "Mateus",
        voice_id: "AByHrwi1S-yLzW-s",
        language: Lang::Pt,
        country: Country::Br,
        gender: Gender::Masculine,
        description: "A bright, welcoming Brazilian Portuguese adult voice with the warmth of a great receptionist. Perfect for greeting customers and friendly front-line support.",
    },
    FlagshipVoice {
        name: "Davi",
        voice_id: "NuUr_x5V90hSHzCJ",
        language: Lang::Pt,
        country: Country::Br,
        gender: Gender::Masculine,
        description: "A confident, upbeat Brazilian Portuguese adult voice with infectious energy. Perfect for proactive assistants and motivating customer success calls.",
    },
    FlagshipVoice {
        name: "Caio",
        voice_id: "Qit9Oc9fEO9yXsVw",
        language: Lang::Pt,
        country: Country::Br,
        gender: Gender::Masculine,
        description: "A sparky, attentive Brazilian Portuguese adult voice that gets to the point with a smile. Ideal for fast, efficient customer support and helpful voice assistants.",
    },
    // Portuguese (Portugal) voices
    FlagshipVoice {
        name: "Sofia",
        voice_id: "tWO-Q6DxWPj7syQA",
        language: Lang::Pt,
        country: Country::Pt,
        gender: Gender::Feminine,
        description: "Pleasant and engaging European Portuguese voice with natural pacing. Ideal for interactive voice assistants and customer service applications.",
    },
    FlagshipVoice {
        name: "Joana",
        voice_id: "7yIuXOsv9bkpmRVv",
        language: Lang::Pt,
        country: Country::Pt,
        gender: Gender::Feminine,
        description: "Friendly and approachable European Portuguese voice with smooth delivery. Great for onboarding and helpful voice assistant interactions.",
    },
    FlagshipVoice {
        name: "Ricardo",
        voice_id: "-n1BmuOkydfLDjxE",
        language: Lang::Pt,
        country: Country::Pt,
        gender: Gender::Masculine,
        description: "Confident and articulate European Portuguese voice with excellent pacing. Ideal for professional presentations and quality voice applications.",
    },
    FlagshipVoice {
        name: "Paulo",
        voice_id: "7iWpEw5Nt05GC1B0",
        language: Lang::Pt,
        country: Country::Pt,
        gender: Gender::Masculine,
        description: "Professional and reliable European Portuguese voice with excellent articulation. Perfect for formal applications and quality assurance materials.",
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
/// let voice = flagship_voice("zoey").unwrap();
/// assert_eq!(voice.voice_id, "NbpkqMVS3CJeq2j8");
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
/// * `gradium_base_url` - Base URL for Gradium services. Falls back to `GRADIUM_BASE_URL` env var, then `https://api.gradium.ai/api`.
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
    /// * `gradium_base_url` - Base URL for Gradium services. Falls back to `GRADIUM_BASE_URL` env var, then `https://api.gradium.ai/api`.
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
        // Default to 0 = "let server decide". Thinking models like MiniMax-M2
        // can blow past hardcoded limits with reasoning tokens, causing truncation.
        let max_completion_tokens = max_completion_tokens.unwrap_or(0);

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
