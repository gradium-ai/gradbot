use pyo3::prelude::*;
use std::sync::Arc;
use tokio::sync::Mutex;

/// Initialize tracing subscriber for logging.
/// Call this once at startup to enable logging via RUST_LOG.
#[pyfunction]
fn init_logging() -> PyResult<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .with_target(true)
        .try_init()
        .ok(); // Ignore error if already initialized
    Ok(())
}

fn to_py_err(e: anyhow::Error) -> PyErr {
    pyo3::exceptions::PyRuntimeError::new_err(e.to_string())
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

impl From<Lang> for gradbot_lib::Lang {
    fn from(lang: Lang) -> Self {
        match lang {
            Lang::En => gradbot_lib::Lang::En,
            Lang::Fr => gradbot_lib::Lang::Fr,
            Lang::Es => gradbot_lib::Lang::Es,
            Lang::De => gradbot_lib::Lang::De,
            Lang::Pt => gradbot_lib::Lang::Pt,
        }
    }
}

impl From<gradbot_lib::Lang> for Lang {
    fn from(lang: gradbot_lib::Lang) -> Self {
        match lang {
            gradbot_lib::Lang::En => Lang::En,
            gradbot_lib::Lang::Fr => Lang::Fr,
            gradbot_lib::Lang::Es => Lang::Es,
            gradbot_lib::Lang::De => Lang::De,
            gradbot_lib::Lang::Pt => Lang::Pt,
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

impl From<gradbot_lib::Gender> for Gender {
    fn from(g: gradbot_lib::Gender) -> Self {
        match g {
            gradbot_lib::Gender::Masculine => Gender::Masculine,
            gradbot_lib::Gender::Feminine => Gender::Feminine,
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

impl From<gradbot_lib::Country> for Country {
    fn from(c: gradbot_lib::Country) -> Self {
        match c {
            gradbot_lib::Country::Us => Country::Us,
            gradbot_lib::Country::Gb => Country::Gb,
            gradbot_lib::Country::Fr => Country::Fr,
            gradbot_lib::Country::De => Country::De,
            gradbot_lib::Country::Mx => Country::Mx,
            gradbot_lib::Country::Es => Country::Es,
            gradbot_lib::Country::Br => Country::Br,
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
    gradbot_lib::flagship_voices()
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
    let voice = gradbot_lib::flagship_voice(name).map_err(to_py_err)?;
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
        Self { name, description, parameters_json }
    }
}

impl ToolDef {
    fn to_lib(&self) -> PyResult<gradbot_lib::ToolDef> {
        let parameters: serde_json::Value =
            serde_json::from_str(&self.parameters_json).map_err(|e| {
                pyo3::exceptions::PyValueError::new_err(format!("Invalid JSON parameters: {}", e))
            })?;
        Ok(gradbot_lib::ToolDef {
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
}

#[pymethods]
impl SessionConfig {
    #[new]
    #[pyo3(signature = (voice_id=None, instructions=None, language=Lang::En, assistant_speaks_first=true, silence_timeout_s=5.0, tools=vec![]))]
    fn new(
        voice_id: Option<String>,
        instructions: Option<String>,
        language: Lang,
        assistant_speaks_first: bool,
        silence_timeout_s: f64,
        tools: Vec<ToolDef>,
    ) -> Self {
        Self { voice_id, instructions, language, assistant_speaks_first, silence_timeout_s, tools }
    }
}

impl SessionConfig {
    fn to_lib(&self) -> PyResult<gradbot_lib::SessionConfig> {
        let tools: PyResult<Vec<_>> = self.tools.iter().map(|t| t.to_lib()).collect();
        Ok(gradbot_lib::SessionConfig {
            voice_id: self.voice_id.clone(),
            instructions: self.instructions.clone(),
            language: self.language.into(),
            assistant_speaks_first: self.assistant_speaks_first,
            silence_timeout_s: self.silence_timeout_s,
            tools: tools?,
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

fn event_from_lib(py: Python<'_>, event: gradbot_lib::Event) -> Event {
    use gradbot_lib::Event::*;
    match event {
        Flushing { started_listening, text_chunks } => {
            let dict = pyo3::types::PyDict::new(py);
            dict.set_item("started_listening", started_listening).ok();
            dict.set_item("text_chunks", text_chunks).ok();
            Event { event_type: "flushing".to_string(), data: Some(dict.unbind().into_any()) }
        }
        EndOfTurn => Event { event_type: "end_of_turn".to_string(), data: None },
        Interrupted => Event { event_type: "interrupted".to_string(), data: None },
        PushToLlm { user_text } => {
            let dict = pyo3::types::PyDict::new(py);
            dict.set_item("user_text", user_text).ok();
            Event { event_type: "push_to_llm".to_string(), data: Some(dict.unbind().into_any()) }
        }
        PreviousLlmGen { agent_text } => {
            let dict = pyo3::types::PyDict::new(py);
            dict.set_item("agent_text", agent_text).ok();
            Event {
                event_type: "previous_llm_gen".to_string(),
                data: Some(dict.unbind().into_any()),
            }
        }
        LlmStarted => Event { event_type: "llm_started".to_string(), data: None },
        FirstWord => Event { event_type: "first_word".to_string(), data: None },
        FirstTtsAudio => Event { event_type: "first_tts_audio".to_string(), data: None },
        EndTtsAudio => Event { event_type: "end_tts_audio".to_string(), data: None },
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

/// Handle for sending tool call results back to the LLM.
#[pyclass]
pub struct ToolCallHandlePy {
    inner: Option<gradbot_lib::ToolCallHandle>,
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
            handle.send(value).await.map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("Failed to send result: {}", e))
            })?;
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
            handle.send_error(anyhow::anyhow!("{}", error_message)).await.map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("Failed to send error: {}", e))
            })?;
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

fn msgout_from_lib(py: Python<'_>, msg: gradbot_lib::MsgOut) -> PyResult<MsgOut> {
    use gradbot_lib::MsgOut::*;
    match msg {
        Audio { data, start_s, stop_s, turn_idx, interrupted } => Ok(MsgOut {
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
        TtsText { text, start_s, stop_s, turn_idx } => Ok(MsgOut {
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
            let handle_py = Py::new(py, ToolCallHandlePy { inner: Some(handle) })?;
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

/// Handle for sending input to a voice session.
#[pyclass]
pub struct SessionInputHandle {
    inner: Arc<Mutex<Option<gradbot_lib::SessionInputHandle>>>,
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
            handle.send_audio(data).await.map_err(to_py_err)?;
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
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let guard = inner.lock().await;
            let handle = guard
                .as_ref()
                .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("session closed"))?;
            handle.send_config(lib_config).await.map_err(to_py_err)?;
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
    inner: Arc<Mutex<Option<gradbot_lib::SessionOutputHandle>>>,
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
            match handle.receive().await {
                Ok(Some(msg)) => Python::with_gil(|py| {
                    let msg_out = msgout_from_lib(py, msg)?;
                    Ok(Some(msg_out))
                }),
                Ok(None) => Ok(None),
                Err(e) => Err(to_py_err(e)),
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
    fn to_encoder_format(self) -> gradbot_lib::encoder::Format {
        match self {
            AudioFormat::OggOpus => gradbot_lib::encoder::Format::OggOpus,
            AudioFormat::Pcm => gradbot_lib::encoder::Format::pcm(OUTPUT_SAMPLE_RATE),
            AudioFormat::Ulaw => gradbot_lib::encoder::Format::ulaw(OUTPUT_SAMPLE_RATE),
        }
    }

    fn to_decoder_format(self) -> gradbot_lib::decoder::Format {
        match self {
            AudioFormat::OggOpus => gradbot_lib::decoder::Format::OggOpus,
            AudioFormat::Pcm => gradbot_lib::decoder::Format::pcm(INPUT_SAMPLE_RATE),
            AudioFormat::Ulaw => gradbot_lib::decoder::Format::ulaw(INPUT_SAMPLE_RATE),
        }
    }
}

/// Shared clients for creating voice AI sessions.
#[pyclass]
pub struct GradbotClients {
    inner: Arc<gradbot_lib::GradbotClients>,
}

/// Create new GradbotClients with optional configuration.
#[pyfunction]
#[pyo3(signature = (gradium_api_key=None, gradium_base_url=None, llm_base_url=None, llm_model_name=None, max_completion_tokens=None))]
fn create_clients<'py>(
    py: Python<'py>,
    gradium_api_key: Option<String>,
    gradium_base_url: Option<String>,
    llm_base_url: Option<String>,
    llm_model_name: Option<String>,
    max_completion_tokens: Option<u32>,
) -> PyResult<Bound<'py, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let clients = gradbot_lib::GradbotClients::new(
            gradium_api_key.as_deref(),
            gradium_base_url.as_deref(),
            llm_base_url.as_deref(),
            llm_model_name.as_deref(),
            max_completion_tokens,
        )
        .await
        .map_err(to_py_err)?;
        Ok(GradbotClients { inner: Arc::new(clients) })
    })
}

#[pymethods]
impl GradbotClients {
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
                    gradbot_lib::IoFormat {
                        input: input_format.to_decoder_format(),
                        output: output_format.to_encoder_format(),
                    },
                )
                .await
                .map_err(to_py_err)?;
            Ok((
                SessionInputHandle { inner: Arc::new(Mutex::new(Some(input))) },
                SessionOutputHandle { inner: Arc::new(Mutex::new(Some(output))) },
            ))
        })
    }
}

/// Create clients and start a session in one call.
///
/// # Arguments
///
/// * `input_format` - Audio format for incoming audio (default: PCM at 24kHz)
/// * `output_format` - Audio format for outgoing audio (default: OggOpus)
#[pyfunction]
#[pyo3(signature = (gradium_api_key=None, gradium_base_url=None, llm_base_url=None, llm_model_name=None, max_completion_tokens=None, session_config=None, input_format=AudioFormat::Pcm, output_format=AudioFormat::OggOpus))]
fn run<'py>(
    py: Python<'py>,
    gradium_api_key: Option<String>,
    gradium_base_url: Option<String>,
    llm_base_url: Option<String>,
    llm_model_name: Option<String>,
    max_completion_tokens: Option<u32>,
    session_config: Option<SessionConfig>,
    input_format: AudioFormat,
    output_format: AudioFormat,
) -> PyResult<Bound<'py, PyAny>> {
    let lib_config = session_config.map(|c| c.to_lib()).transpose()?;
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (input, output) = gradbot_lib::run(
            gradium_api_key.as_deref(),
            gradium_base_url.as_deref(),
            llm_base_url.as_deref(),
            llm_model_name.as_deref(),
            max_completion_tokens,
            lib_config,
            gradbot_lib::IoFormat {
                input: input_format.to_decoder_format(),
                output: output_format.to_encoder_format(),
            },
        )
        .await
        .map_err(to_py_err)?;
        Ok((
            SessionInputHandle { inner: Arc::new(Mutex::new(Some(input))) },
            SessionOutputHandle { inner: Arc::new(Mutex::new(Some(output))) },
        ))
    })
}

/// Python module for gradbot voice AI library.
#[pymodule]
fn pygradbot(m: &Bound<'_, PyModule>) -> PyResult<()> {
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
