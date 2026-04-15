use pyo3::prelude::*;
use std::sync::Arc;
use tokio::sync::Mutex;

mod remote;

/// Initialize tracing subscriber for logging.
/// Call this once at startup to enable logging via RUST_LOG.
#[pyfunction]
fn init_logging() -> PyResult<()> {
    let env_filter = if std::env::var("RUST_LOG").is_ok() {
        tracing_subscriber::EnvFilter::from_default_env()
    } else {
        tracing_subscriber::EnvFilter::new("gradbot=info,gradbot_py=info")
    };
    tracing_subscriber::fmt()
        .with_env_filter(env_filter)
        .with_target(true)
        .try_init()
        .ok(); // Ignore error if already initialized
    Ok(())
}

fn to_py_err(e: anyhow::Error) -> PyErr {
    pyo3::exceptions::PyRuntimeError::new_err(format!("{e:#}"))
}

/// Language enum for voice AI sessions.
#[pyclass(eq, eq_int, hash, frozen)]
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Lang {
    En,
    Fr,
    Es,
    De,
    Pt,
}

#[pymethods]
impl Lang {
    /// Returns the language code (e.g., "en", "fr").
    fn code(&self) -> &str {
        match self {
            Lang::En => "en",
            Lang::Fr => "fr",
            Lang::Es => "es",
            Lang::De => "de",
            Lang::Pt => "pt",
        }
    }

    /// Returns the language code string for use as TTS rewrite_rules.
    #[getter]
    fn rewrite_rules(&self) -> &str {
        self.code()
    }
}

impl From<Lang> for gradbot::Lang {
    fn from(lang: Lang) -> Self {
        match lang {
            Lang::En => gradbot::Lang::En,
            Lang::Fr => gradbot::Lang::Fr,
            Lang::Es => gradbot::Lang::Es,
            Lang::De => gradbot::Lang::De,
            Lang::Pt => gradbot::Lang::Pt,
        }
    }
}

impl From<gradbot::Lang> for Lang {
    fn from(lang: gradbot::Lang) -> Self {
        match lang {
            gradbot::Lang::En => Lang::En,
            gradbot::Lang::Fr => Lang::Fr,
            gradbot::Lang::Es => Lang::Es,
            gradbot::Lang::De => Lang::De,
            gradbot::Lang::Pt => Lang::Pt,
        }
    }
}

/// Gender of a voice.
#[pyclass(eq, eq_int, hash, frozen)]
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Gender {
    Masculine,
    Feminine,
}

impl From<gradbot::Gender> for Gender {
    fn from(g: gradbot::Gender) -> Self {
        match g {
            gradbot::Gender::Masculine => Gender::Masculine,
            gradbot::Gender::Feminine => Gender::Feminine,
        }
    }
}

#[pymethods]
impl Gender {
    fn __str__(&self) -> &'static str {
        match self {
            Gender::Masculine => "Masculine",
            Gender::Feminine => "Feminine",
        }
    }
}

/// Country/accent of a voice.
#[pyclass(eq, eq_int, hash, frozen)]
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Country {
    Us,
    Gb,
    Fr,
    De,
    Mx,
    Es,
    Br,
}

impl From<gradbot::Country> for Country {
    fn from(c: gradbot::Country) -> Self {
        match c {
            gradbot::Country::Us => Country::Us,
            gradbot::Country::Gb => Country::Gb,
            gradbot::Country::Fr => Country::Fr,
            gradbot::Country::De => Country::De,
            gradbot::Country::Mx => Country::Mx,
            gradbot::Country::Es => Country::Es,
            gradbot::Country::Br => Country::Br,
        }
    }
}

#[pymethods]
impl Country {
    fn __str__(&self) -> &'static str {
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

    /// Returns the country code (e.g., "us", "gb").
    fn code(&self) -> &'static str {
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
}

/// Flagship voice information: name, voice ID, language, country, gender, and description.
#[pyclass]
#[derive(Debug, Clone)]
pub struct FlagshipVoice {
    #[pyo3(get)]
    pub name: String,
    #[pyo3(get)]
    pub voice_id: String,
    #[pyo3(get)]
    pub language: Lang,
    #[pyo3(get)]
    pub country: Country,
    #[pyo3(get)]
    pub gender: Gender,
    #[pyo3(get)]
    pub description: String,
}

/// Returns all available flagship voices.
///
/// Example:
///     for voice in flagship_voices():
///         print(f"{voice.name}: {voice.voice_id} ({voice.language})")
#[pyfunction]
fn flagship_voices() -> Vec<FlagshipVoice> {
    gradbot::flagship_voices()
        .iter()
        .map(|v| FlagshipVoice {
            name: v.name.to_string(),
            voice_id: v.voice_id.to_string(),
            language: v.language.into(),
            country: v.country.into(),
            gender: v.gender.into(),
            description: v.description.to_string(),
        })
        .collect()
}

/// Look up a flagship voice by name.
///
/// Returns the voice ID and language for the given voice name.
/// The lookup is case-insensitive.
///
/// Raises RuntimeError if the voice name is not a known flagship voice.
///
/// Example:
///     voice = flagship_voice("emma")
///     print(voice.name)      # "Emma"
///     print(voice.voice_id)  # "YTpq7expH9539ERJ"
///     print(voice.language)  # Lang.En
#[pyfunction]
fn flagship_voice(name: &str) -> PyResult<FlagshipVoice> {
    let voice = gradbot::flagship_voice(name).map_err(to_py_err)?;
    Ok(FlagshipVoice {
        name: voice.name.to_string(),
        voice_id: voice.voice_id.to_string(),
        language: voice.language.into(),
        country: voice.country.into(),
        gender: voice.gender.into(),
        description: voice.description.to_string(),
    })
}

/// Tool definition for the LLM.
///
/// Parameters should be a JSON string representing the JSON Schema for the tool's parameters.
#[pyclass]
#[derive(Debug, Clone)]
pub struct ToolDef {
    #[pyo3(get, set)]
    pub name: String,
    #[pyo3(get, set)]
    pub description: String,
    /// JSON string representing the parameters schema
    #[pyo3(get, set)]
    pub parameters_json: String,
}

#[pymethods]
impl ToolDef {
    #[new]
    fn new(name: String, description: String, parameters_json: String) -> Self {
        Self {
            name,
            description,
            parameters_json,
        }
    }
}

impl ToolDef {
    fn to_lib(&self) -> PyResult<gradbot::ToolDef> {
        let parameters: serde_json::Value =
            serde_json::from_str(&self.parameters_json).map_err(|e| {
                pyo3::exceptions::PyValueError::new_err(format!("Invalid JSON parameters: {}", e))
            })?;
        Ok(gradbot::ToolDef {
            name: self.name.clone(),
            description: self.description.clone(),
            parameters,
        })
    }
}

/// Session configuration for voice AI.
#[pyclass]
#[derive(Debug, Clone)]
pub struct SessionConfig {
    #[pyo3(get, set)]
    pub voice_id: Option<String>,
    #[pyo3(get, set)]
    pub instructions: Option<String>,
    #[pyo3(get, set)]
    pub language: Lang,
    /// If true, the assistant will speak first when the session starts. Defaults to true.
    #[pyo3(get, set)]
    pub assistant_speaks_first: bool,
    /// Seconds of silence after assistant finishes before prompting continuation. Defaults to 3.0.
    #[pyo3(get, set)]
    pub silence_timeout_s: f64,
    /// Tool definitions for the LLM.
    #[pyo3(get, set)]
    pub tools: Vec<ToolDef>,
    /// Duration of silence (in seconds) to flush the STT pipeline. Defaults to 0.5.
    #[pyo3(get, set)]
    pub flush_duration_s: f64,
    /// Padding bonus for STT. Positive = wait longer, negative = finalize sooner. Range: -4 to 4. Default 0.
    #[pyo3(get, set)]
    pub padding_bonus: f64,
    /// TTS rewrite rules. Language codes like "en", "fr" enable all rules for that language.
    #[pyo3(get, set)]
    pub rewrite_rules: Option<String>,
    /// Extra JSON config to merge into the STT stream's json_config.
    #[pyo3(get, set)]
    pub stt_extra_config: Option<String>,
    /// Extra JSON config to merge into the TTS stream's json_config.
    #[pyo3(get, set)]
    pub tts_extra_config: Option<String>,
    /// Extra JSON config to merge into the LLM chat completion request body.
    #[pyo3(get, set)]
    pub llm_extra_config: Option<String>,
}

#[pymethods]
impl SessionConfig {
    #[new]
    #[pyo3(signature = (voice_id=None, instructions=None, language=Lang::En, assistant_speaks_first=true, silence_timeout_s=5.0, tools=vec![], flush_duration_s=0.5, padding_bonus=0.0, rewrite_rules=None, stt_extra_config=None, tts_extra_config=None, llm_extra_config=None))]
    fn new(
        voice_id: Option<String>,
        instructions: Option<String>,
        language: Lang,
        assistant_speaks_first: bool,
        silence_timeout_s: f64,
        tools: Vec<ToolDef>,
        flush_duration_s: f64,
        padding_bonus: f64,
        rewrite_rules: Option<String>,
        stt_extra_config: Option<String>,
        tts_extra_config: Option<String>,
        llm_extra_config: Option<String>,
    ) -> Self {
        Self {
            voice_id,
            instructions,
            language,
            assistant_speaks_first,
            silence_timeout_s,
            tools,
            flush_duration_s,
            padding_bonus,
            rewrite_rules,
            stt_extra_config,
            tts_extra_config,
            llm_extra_config,
        }
    }
}

impl SessionConfig {
    fn to_lib(&self) -> PyResult<gradbot::SessionConfig> {
        let tools: PyResult<Vec<_>> = self.tools.iter().map(|t| t.to_lib()).collect();
        Ok(gradbot::SessionConfig {
            voice_id: self.voice_id.clone(),
            instructions: self.instructions.clone(),
            language: self.language.into(),
            assistant_speaks_first: self.assistant_speaks_first,
            silence_timeout_s: self.silence_timeout_s,
            tools: tools?,
            flush_duration_s: self.flush_duration_s,
            padding_bonus: self.padding_bonus,
            rewrite_rules: self.rewrite_rules.clone(),
            stt_extra_config: self.stt_extra_config.clone(),
            tts_extra_config: self.tts_extra_config.clone(),
            llm_extra_config: self.llm_extra_config.clone(),
        })
    }

    fn to_wire(&self) -> PyResult<remote::SessionConfigWire> {
        let lang_str = match self.language {
            Lang::En => "en",
            Lang::Fr => "fr",
            Lang::Es => "es",
            Lang::De => "de",
            Lang::Pt => "pt",
        };
        let tools: PyResult<Vec<_>> = self
            .tools
            .iter()
            .map(|t| {
                let params: serde_json::Value =
                    serde_json::from_str(&t.parameters_json).map_err(|e| {
                        pyo3::exceptions::PyValueError::new_err(format!(
                            "Invalid JSON parameters: {}",
                            e
                        ))
                    })?;
                Ok(remote::ToolDefWire {
                    name: t.name.clone(),
                    description: t.description.clone(),
                    parameters: params,
                })
            })
            .collect();
        Ok(remote::SessionConfigWire {
            voice_id: self.voice_id.clone(),
            instructions: self.instructions.clone(),
            language: Some(lang_str.to_string()),
            assistant_speaks_first: Some(self.assistant_speaks_first),
            silence_timeout_s: Some(self.silence_timeout_s),
            tools: Some(tools?),
            flush_duration_s: Some(self.flush_duration_s),
            padding_bonus: Some(self.padding_bonus),
            rewrite_rules: self.rewrite_rules.clone(),
            stt_extra_config: self.stt_extra_config.clone(),
            tts_extra_config: self.tts_extra_config.clone(),
            llm_extra_config: self.llm_extra_config.clone(),
        })
    }
}

/// Events emitted during a voice AI session.
#[pyclass]
#[derive(Debug)]
pub struct Event {
    #[pyo3(get)]
    pub event_type: String,
    #[pyo3(get)]
    pub data: Option<PyObject>,
}

fn event_from_lib(py: Python<'_>, event: gradbot::Event) -> Event {
    use gradbot::Event::*;
    match event {
        Flushing {
            started_listening,
            text_chunks,
        } => {
            let dict = pyo3::types::PyDict::new(py);
            dict.set_item("started_listening", started_listening).ok();
            dict.set_item("text_chunks", text_chunks).ok();
            Event {
                event_type: "flushing".to_string(),
                data: Some(dict.unbind().into_any()),
            }
        }
        EndOfTurn => Event {
            event_type: "end_of_turn".to_string(),
            data: None,
        },
        Interrupted => Event {
            event_type: "interrupted".to_string(),
            data: None,
        },
        PushToLlm { user_text } => {
            let dict = pyo3::types::PyDict::new(py);
            dict.set_item("user_text", user_text).ok();
            Event {
                event_type: "push_to_llm".to_string(),
                data: Some(dict.unbind().into_any()),
            }
        }
        PreviousLlmGen { agent_text } => {
            let dict = pyo3::types::PyDict::new(py);
            dict.set_item("agent_text", agent_text).ok();
            Event {
                event_type: "previous_llm_gen".to_string(),
                data: Some(dict.unbind().into_any()),
            }
        }
        LlmStarted => Event {
            event_type: "llm_started".to_string(),
            data: None,
        },
        FirstWord => Event {
            event_type: "first_word".to_string(),
            data: None,
        },
        FirstTtsAudio => Event {
            event_type: "first_tts_audio".to_string(),
            data: None,
        },
        EndTtsAudio => Event {
            event_type: "end_tts_audio".to_string(),
            data: None,
        },
    }
}

/// Tool call information from the LLM.
#[pyclass]
#[derive(Debug, Clone)]
pub struct ToolCallInfo {
    #[pyo3(get)]
    pub call_id: String,
    #[pyo3(get)]
    pub tool_name: String,
    #[pyo3(get)]
    pub args_json: String,
}

// ---------------------------------------------------------------------------
// ToolCallHandle — supports both local and remote modes
// ---------------------------------------------------------------------------

enum ToolCallHandleInner {
    Local(gradbot::ToolCallHandle),
    Remote {
        call_id: String,
        ws_tx: tokio::sync::mpsc::Sender<remote::WsOutMsg>,
    },
}

/// Handle for sending tool call results back to the LLM.
#[pyclass]
pub struct ToolCallHandlePy {
    inner: Option<ToolCallHandleInner>,
}

#[pymethods]
impl ToolCallHandlePy {
    /// Send a successful JSON result for this tool call.
    /// The result should be a JSON string.
    fn send<'py>(&mut self, py: Python<'py>, result_json: String) -> PyResult<Bound<'py, PyAny>> {
        let handle = self
            .inner
            .take()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("handle already used"))?;
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let value: serde_json::Value = serde_json::from_str(&result_json).map_err(|e| {
                pyo3::exceptions::PyValueError::new_err(format!("Invalid JSON: {}", e))
            })?;
            match handle {
                ToolCallHandleInner::Local(h) => {
                    h.send(value).await.map_err(|e| {
                        pyo3::exceptions::PyRuntimeError::new_err(format!(
                            "Failed to send result: {}",
                            e
                        ))
                    })?;
                }
                ToolCallHandleInner::Remote { call_id, ws_tx } => {
                    ws_tx
                        .send(remote::WsOutMsg::ToolResult {
                            call_id,
                            result: value,
                            is_error: false,
                        })
                        .await
                        .map_err(|e| {
                            pyo3::exceptions::PyRuntimeError::new_err(format!(
                                "Failed to send result: {}",
                                e
                            ))
                        })?;
                }
            }
            Ok(())
        })
    }

    /// Send an error result for this tool call.
    fn send_error<'py>(
        &mut self,
        py: Python<'py>,
        error_message: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let handle = self
            .inner
            .take()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("handle already used"))?;
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            match handle {
                ToolCallHandleInner::Local(h) => {
                    h.send_error(anyhow::anyhow!("{}", error_message))
                        .await
                        .map_err(|e| {
                            pyo3::exceptions::PyRuntimeError::new_err(format!(
                                "Failed to send error: {}",
                                e
                            ))
                        })?;
                }
                ToolCallHandleInner::Remote { call_id, ws_tx } => {
                    ws_tx
                        .send(remote::WsOutMsg::ToolResult {
                            call_id,
                            result: serde_json::Value::String(error_message),
                            is_error: true,
                        })
                        .await
                        .map_err(|e| {
                            pyo3::exceptions::PyRuntimeError::new_err(format!(
                                "Failed to send error: {}",
                                e
                            ))
                        })?;
                }
            }
            Ok(())
        })
    }
}

/// Output message from a voice AI session.
#[pyclass]
pub struct MsgOut {
    #[pyo3(get)]
    pub msg_type: String,
    #[pyo3(get)]
    pub data: Option<PyObject>,
    #[pyo3(get)]
    pub text: Option<String>,
    #[pyo3(get)]
    pub start_s: Option<f64>,
    #[pyo3(get)]
    pub stop_s: Option<f64>,
    #[pyo3(get)]
    pub turn_idx: Option<u64>,
    #[pyo3(get)]
    pub time_s: Option<f64>,
    #[pyo3(get)]
    pub event: Option<Py<Event>>,
    #[pyo3(get)]
    pub tool_call: Option<Py<ToolCallInfo>>,
    #[pyo3(get)]
    pub tool_call_handle: Option<Py<ToolCallHandlePy>>,
    /// True when this is the last audio before an interruption (client should fade out slowly).
    #[pyo3(get)]
    pub interrupted: bool,
}

fn msgout_from_lib(py: Python<'_>, msg: gradbot::MsgOut) -> PyResult<MsgOut> {
    use gradbot::MsgOut::*;
    match msg {
        Audio {
            data,
            start_s,
            stop_s,
            turn_idx,
            interrupted,
        } => Ok(MsgOut {
            msg_type: "audio".to_string(),
            data: Some(pyo3::types::PyBytes::new(py, &data).unbind().into_any()),
            text: None,
            start_s: Some(start_s),
            stop_s: Some(stop_s),
            turn_idx: Some(turn_idx),
            time_s: None,
            event: None,
            tool_call: None,
            tool_call_handle: None,
            interrupted,
        }),
        TtsText {
            text,
            start_s,
            stop_s,
            turn_idx,
        } => Ok(MsgOut {
            msg_type: "tts_text".to_string(),
            data: None,
            text: Some(text),
            start_s: Some(start_s),
            stop_s: Some(stop_s),
            turn_idx: Some(turn_idx),
            time_s: None,
            event: None,
            tool_call: None,
            tool_call_handle: None,
            interrupted: false,
        }),
        SttText { text, start_s } => Ok(MsgOut {
            msg_type: "stt_text".to_string(),
            data: None,
            text: Some(text),
            start_s: Some(start_s),
            stop_s: None,
            turn_idx: None,
            time_s: None,
            event: None,
            tool_call: None,
            tool_call_handle: None,
            interrupted: false,
        }),
        Event { time_s, event } => {
            let event_obj = event_from_lib(py, event);
            let event_py = Py::new(py, event_obj)?;
            Ok(MsgOut {
                msg_type: "event".to_string(),
                data: None,
                text: None,
                start_s: None,
                stop_s: None,
                turn_idx: None,
                time_s: Some(time_s),
                event: Some(event_py),
                tool_call: None,
                tool_call_handle: None,
                interrupted: false,
            })
        }
        ToolCall { call, handle } => {
            let tool_call_info = ToolCallInfo {
                call_id: call.call_id,
                tool_name: call.tool_name,
                args_json: call.args.to_string(),
            };
            let tool_call_py = Py::new(py, tool_call_info)?;
            let handle_py = Py::new(
                py,
                ToolCallHandlePy {
                    inner: Some(ToolCallHandleInner::Local(handle)),
                },
            )?;
            Ok(MsgOut {
                msg_type: "tool_call".to_string(),
                data: None,
                text: None,
                start_s: None,
                stop_s: None,
                turn_idx: None,
                time_s: None,
                event: None,
                tool_call: Some(tool_call_py),
                tool_call_handle: Some(handle_py),
                interrupted: false,
            })
        }
    }
}

// ---------------------------------------------------------------------------
// Input/Output handles — support both local and remote modes
// ---------------------------------------------------------------------------

enum InputHandleInner {
    Local(gradbot::SessionInputHandle),
    Remote(remote::RemoteInputHandle),
}

enum OutputHandleInner {
    Local(gradbot::SessionOutputHandle),
    Remote(remote::RemoteOutputHandle),
}

/// Handle for sending input to a voice session.
#[pyclass]
pub struct SessionInputHandle {
    inner: Arc<Mutex<Option<InputHandleInner>>>,
}

#[pymethods]
impl SessionInputHandle {
    /// Send encoded audio data to the session.
    fn send_audio<'py>(&self, py: Python<'py>, data: Vec<u8>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let guard = inner.lock().await;
            let handle = guard
                .as_ref()
                .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("session closed"))?;
            match handle {
                InputHandleInner::Local(h) => h.send_audio(data).await.map_err(to_py_err)?,
                InputHandleInner::Remote(h) => h.send_audio(data).await.map_err(to_py_err)?,
            }
            Ok(())
        })
    }

    /// Send or update the session configuration.
    fn send_config<'py>(
        &self,
        py: Python<'py>,
        config: SessionConfig,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        let lib_config = config.to_lib()?;
        let wire_config = config.to_wire()?;
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let guard = inner.lock().await;
            let handle = guard
                .as_ref()
                .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("session closed"))?;
            match handle {
                InputHandleInner::Local(h) => h.send_config(lib_config).await.map_err(to_py_err)?,
                InputHandleInner::Remote(h) => {
                    h.send_config(wire_config).await.map_err(to_py_err)?
                }
            }
            Ok(())
        })
    }

    /// Close the input handle.
    fn close<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let mut guard = inner.lock().await;
            *guard = None;
            Ok(())
        })
    }
}

/// Handle for receiving output from a voice session.
#[pyclass]
pub struct SessionOutputHandle {
    inner: Arc<Mutex<Option<OutputHandleInner>>>,
}

#[pymethods]
impl SessionOutputHandle {
    /// Receive the next outbound message from the session.
    ///
    /// Returns None when the session ends normally.
    fn receive<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let mut guard = inner.lock().await;
            let handle = guard
                .as_mut()
                .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("session closed"))?;
            match handle {
                OutputHandleInner::Local(h) => match h.receive().await {
                    Ok(Some(msg)) => Python::with_gil(|py| {
                        let msg_out = msgout_from_lib(py, msg)?;
                        Ok(Some(msg_out))
                    }),
                    Ok(None) => Ok(None),
                    Err(e) => Err(to_py_err(e)),
                },
                OutputHandleInner::Remote(h) => match h.receive().await {
                    Some(msg) => Python::with_gil(|py| {
                        let msg_out = remote::msgout_from_remote(py, msg, h.ws_tx())?;
                        Ok(Some(msg_out))
                    }),
                    None => Ok(None),
                },
            }
        })
    }
}

/// Audio format for encoding/decoding.
#[pyclass(eq, eq_int, hash, frozen)]
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum AudioFormat {
    OggOpus,
    Pcm,
    Ulaw,
}

const INPUT_SAMPLE_RATE: usize = 24000;
const OUTPUT_SAMPLE_RATE: usize = 48000;

impl AudioFormat {
    fn to_encoder_format(self) -> gradbot::encoder::Format {
        match self {
            AudioFormat::OggOpus => gradbot::encoder::Format::OggOpus,
            AudioFormat::Pcm => gradbot::encoder::Format::pcm(OUTPUT_SAMPLE_RATE),
            AudioFormat::Ulaw => gradbot::encoder::Format::ulaw(OUTPUT_SAMPLE_RATE),
        }
    }

    fn to_decoder_format(self) -> gradbot::decoder::Format {
        match self {
            AudioFormat::OggOpus => gradbot::decoder::Format::OggOpus,
            AudioFormat::Pcm => gradbot::decoder::Format::pcm(INPUT_SAMPLE_RATE),
            AudioFormat::Ulaw => gradbot::decoder::Format::ulaw(INPUT_SAMPLE_RATE),
        }
    }
}

/// Shared clients for creating voice AI sessions.
#[pyclass]
pub struct GradbotClients {
    inner: Arc<gradbot::GradbotClients>,
}

/// Create new GradbotClients with optional configuration.
#[pyfunction]
#[pyo3(signature = (gradium_api_key=None, gradium_base_url=None, llm_base_url=None, llm_model_name=None, llm_api_key=None, max_completion_tokens=None))]
fn create_clients<'py>(
    py: Python<'py>,
    gradium_api_key: Option<String>,
    gradium_base_url: Option<String>,
    llm_base_url: Option<String>,
    llm_model_name: Option<String>,
    llm_api_key: Option<String>,
    max_completion_tokens: Option<u32>,
) -> PyResult<Bound<'py, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let clients = gradbot::GradbotClients::new(
            gradium_api_key.as_deref(),
            gradium_base_url.as_deref(),
            llm_base_url.as_deref(),
            llm_model_name.as_deref(),
            llm_api_key.as_deref(),
            max_completion_tokens,
        )
        .await
        .map_err(to_py_err)?;
        Ok(GradbotClients {
            inner: Arc::new(clients),
        })
    })
}

#[pymethods]
impl GradbotClients {
    /// Synthesize text to OggOpus audio without using an LLM.
    ///
    /// Returns a list of (audio_bytes, text, start_s, stop_s) tuples.
    /// Audio chunks are OggOpus encoded. Text chunks contain the spoken words.
    #[pyo3(signature = (text, voice_id=None, rewrite_rules=None))]
    fn tts_synthesize<'py>(
        &self,
        py: Python<'py>,
        text: String,
        voice_id: Option<String>,
        rewrite_rules: Option<String>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let (mut tts_tx, mut tts_rx) = inner
                .tts_client()
                .tts_stream(
                    std::env::var("GRADIUM_TTS_MODEL_NAME").ok(),
                    voice_id,
                    0.0,
                    rewrite_rules,
                    None,
                )
                .await
                .map_err(to_py_err)?;

            // Create OggOpus encoder (format, frame_size, sample_rate)
            let mut encoder = gradbot::encoder::Encoder::new(
                gradbot::encoder::Format::OggOpus,
                gradbot::OUTPUT_FRAME_SIZE,
                gradbot::OUTPUT_SAMPLE_RATE,
            )
            .map_err(to_py_err)?;

            // Send text and close
            tts_tx.send_text(&text).await.map_err(to_py_err)?;
            tts_tx.send_end_of_stream().await.map_err(to_py_err)?;

            // Collect raw data in the async block (no Python GIL needed)
            // Each entry: (audio_data, text, start_s, stop_s)
            let mut chunks: Vec<(Vec<u8>, String, f64, f64)> = Vec::new();

            // OggOpus header first
            if let Some(header) = encoder.header() {
                chunks.push((header.to_vec(), String::new(), 0.0, 0.0));
            }

            while let Some(msg) = tts_rx.next_message(0).await.map_err(to_py_err)? {
                match msg {
                    gradbot::text_to_speech::TtsOut::Audio {
                        pcm,
                        start_s,
                        stop_s,
                        ..
                    } => {
                        let encoded = encoder.encode(&pcm).map_err(to_py_err)?;
                        if !encoded.data.is_empty() {
                            chunks.push((encoded.data, String::new(), start_s, stop_s));
                        }
                    }
                    gradbot::text_to_speech::TtsOut::Text {
                        text,
                        start_s,
                        stop_s,
                        ..
                    } => {
                        chunks.push((Vec::new(), text, start_s, stop_s));
                    }
                    gradbot::text_to_speech::TtsOut::TurnComplete { .. } => break,
                }
            }

            // Convert to Python objects
            Ok(chunks)
        })
    }

    /// Start a new voice AI session.
    ///
    /// # Arguments
    ///
    /// * `initial_config` - Optional session configuration (voice, language, instructions)
    /// * `input_format` - Audio format for incoming audio (default: PCM at 24kHz)
    /// * `output_format` - Audio format for outgoing audio (default: OggOpus)
    #[pyo3(signature = (initial_config=None, input_format=AudioFormat::Pcm, output_format=AudioFormat::OggOpus))]
    fn start_session<'py>(
        &self,
        py: Python<'py>,
        initial_config: Option<SessionConfig>,
        input_format: AudioFormat,
        output_format: AudioFormat,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        let lib_config = initial_config.map(|c| c.to_lib()).transpose()?;
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let (input, output) = inner
                .start_session(
                    lib_config,
                    gradbot::IoFormat {
                        input: input_format.to_decoder_format(),
                        output: output_format.to_encoder_format(),
                    },
                )
                .await
                .map_err(to_py_err)?;
            Ok((
                SessionInputHandle {
                    inner: Arc::new(Mutex::new(Some(InputHandleInner::Local(input)))),
                },
                SessionOutputHandle {
                    inner: Arc::new(Mutex::new(Some(OutputHandleInner::Local(output)))),
                },
            ))
        })
    }
}

/// Create clients and start a session in one call.
///
/// When `gradbot_url` is provided, connects to a remote gradbot_server instead of
/// creating local STT/LLM/TTS clients. The session behaves identically from the
/// caller's perspective.
///
/// # Arguments
///
/// * `gradbot_url` - WebSocket URL of a gradbot_server (e.g. "wss://server.com/ws"). Enables remote mode.
/// * `gradbot_api_key` - API key sent as Bearer token to gradbot_server (used for STT/TTS billing).
/// * `input_format` - Audio format for incoming audio (default: PCM at 24kHz)
/// * `output_format` - Audio format for outgoing audio (default: OggOpus)
#[pyfunction]
#[pyo3(signature = (gradium_api_key=None, gradium_base_url=None, llm_base_url=None, llm_model_name=None, llm_api_key=None, max_completion_tokens=None, session_config=None, input_format=AudioFormat::Pcm, output_format=AudioFormat::OggOpus, gradbot_url=None, gradbot_api_key=None))]
fn run<'py>(
    py: Python<'py>,
    gradium_api_key: Option<String>,
    gradium_base_url: Option<String>,
    llm_base_url: Option<String>,
    llm_model_name: Option<String>,
    llm_api_key: Option<String>,
    max_completion_tokens: Option<u32>,
    session_config: Option<SessionConfig>,
    input_format: AudioFormat,
    output_format: AudioFormat,
    gradbot_url: Option<String>,
    gradbot_api_key: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    // Remote mode
    if let Some(url) = gradbot_url {
        let wire_config = session_config.as_ref().map(|c| c.to_wire()).transpose()?;
        return pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let (remote_input, remote_output) = remote::connect(url, gradbot_api_key, wire_config)
                .await
                .map_err(to_py_err)?;
            Ok((
                SessionInputHandle {
                    inner: Arc::new(Mutex::new(Some(InputHandleInner::Remote(remote_input)))),
                },
                SessionOutputHandle {
                    inner: Arc::new(Mutex::new(Some(OutputHandleInner::Remote(remote_output)))),
                },
            ))
        });
    }

    // Local mode (original behavior)
    let lib_config = session_config.map(|c| c.to_lib()).transpose()?;
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (input, output) = gradbot::run(
            gradium_api_key.as_deref(),
            gradium_base_url.as_deref(),
            llm_base_url.as_deref(),
            llm_model_name.as_deref(),
            llm_api_key.as_deref(),
            max_completion_tokens,
            lib_config,
            gradbot::IoFormat {
                input: input_format.to_decoder_format(),
                output: output_format.to_encoder_format(),
            },
        )
        .await
        .map_err(to_py_err)?;
        Ok((
            SessionInputHandle {
                inner: Arc::new(Mutex::new(Some(InputHandleInner::Local(input)))),
            },
            SessionOutputHandle {
                inner: Arc::new(Mutex::new(Some(OutputHandleInner::Local(output)))),
            },
        ))
    })
}

/// Python module for gradbot voice AI library.
#[pymodule(name = "_gradbot")]
fn gradbot_py(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Lang>()?;
    m.add_class::<Gender>()?;
    m.add_class::<Country>()?;
    m.add_class::<FlagshipVoice>()?;
    m.add_class::<ToolDef>()?;
    m.add_class::<ToolCallInfo>()?;
    m.add_class::<ToolCallHandlePy>()?;
    m.add_class::<SessionConfig>()?;
    m.add_class::<Event>()?;
    m.add_class::<MsgOut>()?;
    m.add_class::<SessionInputHandle>()?;
    m.add_class::<SessionOutputHandle>()?;
    m.add_class::<AudioFormat>()?;
    m.add_class::<GradbotClients>()?;
    m.add_function(wrap_pyfunction!(init_logging, m)?)?;
    m.add_function(wrap_pyfunction!(flagship_voices, m)?)?;
    m.add_function(wrap_pyfunction!(flagship_voice, m)?)?;
    m.add_function(wrap_pyfunction!(create_clients, m)?)?;
    m.add_function(wrap_pyfunction!(run, m)?)?;
    Ok(())
}
