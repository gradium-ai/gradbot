use crate::speech_to_text::{SttClient, SttStreamReceiver, SttStreamSender};
use crate::text_to_speech::{TtsClient, TtsOut};
use anyhow::{Context, Result};
use std::sync::Arc;
use tokio::sync::Mutex;

pub const DEFAULT_FLUSH_FOR_S: f64 = 0.5;
const INPUT_SAMPLE_RATE: usize = 24000;

/// Internal commands sent from spawned tasks back to the main session loop.
enum InternalCmd {
    /// Restart the STT stream to clear stuck state.
    RestartStt,
    /// Trigger the initial greeting (assistant speaks first).
    Greet,
}

/// Returns the internal reset_asr tool definition.
/// This tool allows the LLM to reset the speech-to-text system when it gets stuck.
fn reset_asr_tool() -> crate::llm::ToolDef {
    crate::llm::ToolDef {
        name: "reset_asr".to_string(),
        description: "Reset the speech recognition system. Use this when the user seems to be \
            repeating themselves and the transcription keeps giving the same nonsensical result - \
            the ASR may be stuck in a bad state. After calling this, the next transcription may \
            start mid-sentence or lack context from what the user just said, which is expected."
            .to_string(),
        parameters: serde_json::json!({
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Brief description of why you're resetting the ASR"
                }
            },
            "required": []
        }),
    }
}
const INPUT_FRAME_SIZE: usize = 1920;
pub const OUTPUT_SAMPLE_RATE: usize = 48000;
pub const OUTPUT_FRAME_SIZE: usize = 3840;

/// Status for tracking how an audio stream interruption should be handled.
/// Channel capacity for inbound messages (audio from client).
/// At ~50 frames/sec, 200 frames ≈ 4 seconds of buffer.
pub const MSG_IN_CHANNEL_CAPACITY: usize = 200;
/// Channel capacity for outbound messages (audio/text to client).
pub const MSG_OUT_CHANNEL_CAPACITY: usize = 100;
/// Channel capacity for internal TTS output queue.
const TTS_OUT_CHANNEL_CAPACITY: usize = 100;

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(tag = "type")]
pub enum Event {
    Flushing {
        started_listening: f64,
        text_chunks: usize,
    },
    EndOfTurn,
    Interrupted,
    PushToLlm {
        user_text: String,
    },
    PreviousLlmGen {
        agent_text: String,
    },
    LlmStarted,
    FirstWord,
    FirstTtsAudio,
    EndTtsAudio,
}

#[allow(dead_code)]
enum State {
    // All seconds in time of the current STT
    Listening {
        since_s: f64,
        texts: Vec<String>,
        turn_idx: u64,
    },
    Flushing {
        since_s: f64,
        flush_duration_s: f64,
        texts: Vec<String>,
        turn_idx: u64,
    },
    Processing {
        since_s: f64,
        turn_idx: u64, // which turn of the user is it
        /// Join handle for the LLM/TTS task. Kept here to keep the task alive (aborts on drop).
        _jh: crate::utils::JoinHandleAbortOnDrop,
    },
}

impl State {
    fn turn_idx(&self) -> u64 {
        match self {
            State::Listening { turn_idx, .. }
            | State::Flushing { turn_idx, .. }
            | State::Processing { turn_idx, .. } => *turn_idx,
        }
    }
}

struct SttSender_ {
    ss: SttStreamSender,
    samples_sent: u64,
    flush_samples_sent: u64,
}

#[derive(Clone)]
struct SttSender(Arc<Mutex<SttSender_>>);

struct Session {
    tts_client: Arc<TtsClient>,
    stt_client: Arc<SttClient>,
    stt_receiver: SttStreamReceiver,
    llm: Arc<tokio::sync::RwLock<crate::llm::LlmSession>>,
    /// Channel for TTS audio/text with timing (processed by out_send_loop).
    /// Errors and TurnComplete signals also go through here.
    tts_out_tx: tokio::sync::mpsc::Sender<Result<TtsOut>>,
    /// Channel for immediate sends (SttText, Events) - goes directly to client.
    msg_out_tx: tokio::sync::mpsc::Sender<MsgOut>,
    /// Shared state - accessed by both transcription loop and out_send_loop.
    state: Arc<Mutex<State>>,
    stt_sender: SttSender,
    session_config: Arc<Mutex<Option<SessionConfig>>>,
    llm_config: Option<Arc<crate::llm::LlmConfig>>,
    /// Counter for consecutive "..." silence prompts. Reset when user speaks. Capped at 5.
    silence_prompts: u32,
    /// Session time (in audio_time_s units) when the current STT stream was started.
    ///
    /// # Timing Model
    ///
    /// We maintain two time references:
    /// - `audio_time_s()`: Master clock based on samples sent to STT. Monotonically increasing,
    ///   never resets. This is our "session time".
    /// - STT-reported times (`current_s`, `start_s`): Relative to when the current STT stream
    ///   started. Resets to 0 each time we call `stt_client.stt_stream()`.
    ///
    /// To convert STT times to session time: `stt_stream_start_audio_s + stt_reported_time`
    ///
    /// This preserves the precision of STT timestamps (which reflect when speech actually
    /// occurred in the audio) while anchoring them to our master clock.
    stt_stream_start_audio_s: f64,
    /// Channel for internal commands from spawned tasks (e.g., reset_asr tool).
    internal_cmd_rx: tokio::sync::mpsc::Receiver<InternalCmd>,
    /// Sender for internal commands - cloned into spawned tasks.
    internal_cmd_tx: tokio::sync::mpsc::Sender<InternalCmd>,
    /// Wall-clock time when the current STT connection was established.
    /// Used to auto-reconnect if disconnected after being connected for >20s.
    stt_connected_at: std::time::Instant,
    /// Minimum VAD inactivity probability observed since last reset/text.
    min_inactivity_prob: f64,
    /// Last VAD inactivity probability received from STT.
    last_inactivity_prob: f64,
    /// Whether non-LLM/non-silence STT restarts are enabled.
    restart_stt_enabled: bool,
    /// Signal to the LLM/TTS task that the user interrupted.
    /// The task checks this and breaks after sending one more audio with interrupted=true.
    user_interrupted: Arc<std::sync::atomic::AtomicBool>,
    /// Holds the JoinHandle of a user-interrupted LLM/TTS task while it winds down.
    /// Dropped when the next task starts.
    interrupted_task_jh: Option<crate::utils::JoinHandleAbortOnDrop>,
}

impl Session {
    async fn new(
        tts_client: Arc<TtsClient>,
        stt_client: Arc<SttClient>,
        llm: Arc<crate::llm::Llm>,
        tts_out_tx: tokio::sync::mpsc::Sender<Result<TtsOut>>,
        msg_out_tx: tokio::sync::mpsc::Sender<MsgOut>,
        session_config: Option<SessionConfig>,
    ) -> Result<(Self, SttSender)> {
        let stt_lang = session_config
            .as_ref()
            .map(|c| c.language)
            .unwrap_or(crate::Lang::En);
        let stt_extra = session_config
            .as_ref()
            .and_then(|c| c.stt_extra_config.as_deref());
        let stt_model_name = std::env::var("GRADIUM_STT_MODEL_NAME").ok();
        let (ss, stt_receiver) = stt_client
            .stt_stream(stt_model_name, stt_lang, stt_extra)
            .await
            .context("STT: failed to create stream")?;
        let llm = Arc::new(tokio::sync::RwLock::new(
            llm.session().context("LLM: failed to create session")?,
        ));
        let stt_sender = SttSender_ {
            ss,
            samples_sent: 0,
            flush_samples_sent: 0,
        };
        let stt_sender = SttSender(Arc::new(Mutex::new(stt_sender)));
        let session_config = Arc::new(Mutex::new(session_config));
        let (internal_cmd_tx, internal_cmd_rx) = tokio::sync::mpsc::channel(10);
        let slf = Self {
            tts_client,
            stt_client,
            llm,
            stt_receiver,
            state: Arc::new(Mutex::new(State::Listening {
                since_s: 0.0,
                texts: vec![],
                turn_idx: 0,
            })),
            tts_out_tx,
            msg_out_tx,
            stt_sender: stt_sender.clone(),
            session_config,
            llm_config: None,
            silence_prompts: 0,
            internal_cmd_rx,
            internal_cmd_tx,
            // First STT stream starts at session time 0
            stt_stream_start_audio_s: 0.0,
            stt_connected_at: std::time::Instant::now(),
            min_inactivity_prob: 1.0,
            last_inactivity_prob: 1.0,
            restart_stt_enabled: std::env::var("RESTART_STT")
                .map(|v| {
                    matches!(
                        v.as_str(),
                        "1" | "true" | "TRUE" | "yes" | "YES" | "on" | "ON"
                    )
                })
                .unwrap_or(false),
            user_interrupted: Arc::new(std::sync::atomic::AtomicBool::new(false)),
            interrupted_task_jh: None,
        };
        Ok((slf, stt_sender))
    }

    /// Restart the STT stream. Called after "..." prompts to reset STT state.
    ///
    /// The new STT stream will report times starting from 0. We anchor these to our
    /// session timeline by recording the current audio_time_s as the stream's start time.
    async fn restart_stt(&mut self) -> Result<()> {
        let audio_time = self.audio_time_s().await;
        tracing::info!(audio_time_s = audio_time, "restarting STT stream");

        // Anchor the new STT stream to our session timeline
        self.stt_stream_start_audio_s = audio_time;
        self.min_inactivity_prob = 1.0;
        self.last_inactivity_prob = 1.0;

        let config_guard = self.session_config.lock().await;
        let stt_lang = config_guard
            .as_ref()
            .map(|c| c.language)
            .unwrap_or(crate::Lang::En);
        let stt_extra = config_guard
            .as_ref()
            .and_then(|c| c.stt_extra_config.as_deref());
        let stt_model_name = std::env::var("GRADIUM_STT_MODEL_NAME").ok();
        let (ss, stt_receiver) = self
            .stt_client
            .stt_stream(stt_model_name, stt_lang, stt_extra)
            .await
            .context("STT: failed to reconnect stream")?;
        drop(config_guard);
        self.stt_receiver = stt_receiver;
        self.stt_connected_at = std::time::Instant::now();
        let mut sender = self.stt_sender.0.lock().await;
        sender.ss = ss;
        Ok(())
    }

    /// Returns the current audio time in seconds, based on samples sent to STT.
    ///
    /// This is the master clock for the session - monotonically increasing, never resets.
    /// All timestamps in events and state should ultimately be in this timeline.
    async fn audio_time_s(&self) -> f64 {
        let samples = self.stt_sender.0.lock().await.samples_sent;
        samples as f64 / INPUT_SAMPLE_RATE as f64
    }

    /// Convert an STT-reported timestamp to session time.
    ///
    /// STT reports times relative to when its current stream started (resets to 0 on restart).
    /// This method converts those to session time by adding the stream's start offset.
    ///
    /// Use this for timestamps that come from STT (e.g., when a word was spoken) to get
    /// accurate session-relative timing while preserving the precision of STT's timestamps.
    fn stt_to_session_time(&self, stt_time: f64) -> f64 {
        self.stt_stream_start_audio_s + stt_time
    }

    /// Minimum connection duration before auto-reconnect is allowed.
    /// Prevents infinite reconnect loops if the server keeps rejecting us.
    ///
    /// Note: STT sessions are currently capped at 5 minutes on the server side,
    /// so we handle automatic reconnection to support longer conversations.
    const MIN_CONNECTION_DURATION_FOR_RECONNECT: std::time::Duration =
        std::time::Duration::from_secs(20);

    pub async fn transcription_receive_loop(&mut self) -> Result<()> {
        use crate::speech_to_text::Msg;
        loop {
            tokio::select! {
                // Handle STT messages
                event = self.stt_receiver.next_message() => {
                    let event = match event {
                        Ok(Some(e)) => e,
                        Ok(None) => {
                            // STT stream ended - auto-reconnect if connected long enough
                            let connected_for = self.stt_connected_at.elapsed();
                            if connected_for >= Self::MIN_CONNECTION_DURATION_FOR_RECONNECT {
                                tracing::info!(
                                    connected_for_secs = connected_for.as_secs(),
                                    "STT stream ended, auto-reconnecting"
                                );
                                self.restart_stt().await?;
                                continue;
                            } else {
                                tracing::info!(
                                    connected_for_secs = connected_for.as_secs(),
                                    "STT stream ended after short connection, not reconnecting"
                                );
                                break;
                            }
                        }
                        Err(e) => {
                            // STT error - auto-reconnect if connected long enough
                            let connected_for = self.stt_connected_at.elapsed();
                            if connected_for >= Self::MIN_CONNECTION_DURATION_FOR_RECONNECT {
                                tracing::warn!(
                                    ?e,
                                    connected_for_secs = connected_for.as_secs(),
                                    "STT stream error, auto-reconnecting"
                                );
                                self.restart_stt().await?;
                                continue;
                            } else {
                                tracing::error!(
                                    ?e,
                                    connected_for_secs = connected_for.as_secs(),
                                    "STT stream error after short connection, not reconnecting"
                                );
                                return Err(e);
                            }
                        }
                    };
                    match event {
                        Msg::Step { end_of_turn, current_s, inactivity_prob } => {
                            // Convert STT-reported time to session time.
                            // current_s tells us where STT is in processing the audio - this may lag
                            // behind audio_time_s() if STT has buffering/processing delay.
                            let session_time = self.stt_to_session_time(current_s);
                            self.last_inactivity_prob = inactivity_prob;
                            if inactivity_prob < self.min_inactivity_prob {
                                self.min_inactivity_prob = inactivity_prob;
                            }
                            self.on_step(session_time, end_of_turn, inactivity_prob).await?;
                            if end_of_turn {
                                self.on_end_of_turn(session_time).await?
                            }
                        }
                        Msg::Text { text, start_s } => {
                            // Convert STT-reported start time to session time.
                            // start_s is when STT detected the speech started - more accurate than
                            // audio_time_s() for knowing when the user actually spoke.
                            let session_time = self.stt_to_session_time(start_s);
                            self.on_text(text, session_time).await?
                        }
                    }
                }
                // Handle internal commands from spawned tasks
                cmd = self.internal_cmd_rx.recv() => {
                    match cmd {
                        Some(InternalCmd::RestartStt) => {
                            tracing::info!("reset_asr tool: restarting STT stream");
                            self.restart_stt().await?;
                        }
                        Some(InternalCmd::Greet) => {
                            tracing::info!("assistant_speaks_first: triggering initial greeting");
                            if let Some(jh) = self.llm_tts("[start]", 0).await {
                                *self.state.lock().await = State::Processing {
                                    since_s: 0.0,
                                    turn_idx: 0,
                                    _jh: jh,
                                };
                            }
                        }
                        None => break, // Channel closed
                    }
                }
            }
        }
        Ok(())
    }

    // This function handles the full logic for a turn, deciding to stop if it receives a tool or a user interuption.
    async fn llm_tts(
        &mut self,
        text: &str,
        turn_idx: u64,
    ) -> Option<crate::utils::JoinHandleAbortOnDrop> {
        // Build/update LlmConfig from SessionConfig
        let (llm_config, llm_extra_config) = {
            let config_guard = self.session_config.lock().await;
            let config = config_guard.as_ref()?;
            let user_prompt = config.instructions.clone().unwrap_or_default();
            let language = config.language;
            let llm_extra_config = config.llm_extra_config.clone();
            // Inject the internal reset_asr tool alongside user-provided tools
            let mut tools = config.tools.clone();
            tools.push(reset_asr_tool());
            let llm_config = match &self.llm_config {
                Some(existing) => {
                    crate::llm::LlmConfig::maybe_update(existing, &user_prompt, language, tools)
                }
                None => Arc::new(crate::llm::LlmConfig::new(user_prompt, language, tools)),
            };
            (llm_config, llm_extra_config)
        };
        self.llm_config = Some(llm_config.clone());

        let previous_gen = match self
            .llm
            .write()
            .await
            .incorporate_previous_generation()
            .await
        {
            Ok(prev) => prev,
            Err(e) => {
                tracing::error!(?e, "failed to incorporate previous generation");
                return None;
            }
        };
        if let Some(previous_gen) = previous_gen {
            let _ = self
                .send_event(Event::PreviousLlmGen {
                    agent_text: previous_gen,
                })
                .await;
        }
        let _ = self
            .send_event(Event::PushToLlm {
                user_text: text.to_string(),
            })
            .await;
        let streaming_session = match self
            .llm
            .write()
            .await
            .push(text, llm_config, llm_extra_config.as_deref())
            .await
            .context("LLM: failed to push text")
        {
            Ok(session) => session,
            Err(e) => {
                tracing::error!(?e, "LLM: failed to push text");
                return None;
            }
        };
        // Drop any previous interrupted task and reset the flag
        self.interrupted_task_jh = None;
        self.user_interrupted
            .store(false, std::sync::atomic::Ordering::Release);

        let tts_client = self.tts_client.clone();
        let tts_out_tx = self.tts_out_tx.clone();
        let msg_out_tx = self.msg_out_tx.clone();
        let start_time = self.audio_time_s().await;
        let llm_request_start = std::time::Instant::now();
        let _ = self.send_event(Event::LlmStarted).await;
        let stt_sender = self.stt_sender.clone();
        let session_config = self.session_config.clone();
        let internal_cmd_tx = self.internal_cmd_tx.clone();
        let llm = self.llm.clone();
        let user_interrupted = self.user_interrupted.clone();
        let (padding_bonus, rewrite_rules, tts_extra_config) = {
            let guard = self.session_config.lock().await;
            (
                guard.as_ref().map(|c| c.padding_bonus).unwrap_or(0.0),
                guard.as_ref().and_then(|c| c.rewrite_rules.clone()),
                guard.as_ref().and_then(|c| c.tts_extra_config.clone()),
            )
        };
        let jh = crate::utils::spawn_abort_on_drop("llm-tts", async move {
            let result: Result<f64> = async {
                let voice_id = {
                    let config_guard = session_config.lock().await;
                    let config = config_guard.as_ref().ok_or_else(|| {
                        anyhow::anyhow!("Session configuration required before audio processing")
                    })?;
                    config.voice_id.clone()
                };
                tracing::info!(?voice_id, "creating TTS stream");
                let tts_model_name = std::env::var("GRADIUM_TTS_MODEL_NAME").ok();
                let (mut tts_tx, tts_rx) =
                    tts_client.tts_stream(tts_model_name, voice_id.clone(), padding_bonus, rewrite_rules.clone(), tts_extra_config.as_deref()).await
                        .context("TTS: failed to create stream")?;
                tracing::info!("TTS stream created successfully");
                // Shared state for tracking last stop_s across futures
                let last_stop_s =
                    Arc::new(std::sync::atomic::AtomicU64::new(start_time.to_bits()));
                let llm_to_tts = {
                    let msg_out_tx = msg_out_tx.clone();
                    let stt_sender = stt_sender.clone();
                    let internal_cmd_tx = internal_cmd_tx.clone();
                    async move {
                        let mut streaming_session = streaming_session;
                        let mut first_word = true;
                        let mut buffer = String::new();
                        // When the buffer ends with "<digit>." or "<digit>,", we can't
                        // tell if the punctuation is mid-number ($50,000 or 6.1%) or a
                        // real boundary (sentence end / list comma). We stash the buffer
                        // and decide when the next chunk arrives.
                        let mut pending_numeric: Option<String> = None;
                        let mut tool_call_as_text = false;
                        while let Some(item) = streaming_session.recv().await {
                            tracing::debug!(?item, "LLM stream item received");
                            match item {
                                crate::llm::LlmResponseItem::Text(chunk) => {
                                    // Detect tool-call-as-text: some models (e.g. Qwen) may
                                    // output tool calls as plain text instead of structured
                                    // tool_calls. Once detected, suppress ALL remaining text
                                    // for this LLM turn (content between tags is clean text
                                    // that would otherwise leak to TTS).
                                    if tool_call_as_text {
                                        tracing::debug!(
                                            text = %chunk,
                                            "Suppressing text (tool-call-as-text mode active)"
                                        );
                                        continue;
                                    }
                                    if chunk.contains("<tool_call>")
                                        || chunk.contains("</tool_call>")
                                        || chunk.contains("<function=")
                                        || chunk.contains("</function>")
                                        || chunk.contains("<parameter=")
                                        || chunk.contains("</parameter>")
                                        || chunk.contains("\"tool_calls\"")
                                    {
                                        tracing::warn!(
                                            text = %chunk,
                                            "LLM emitted tool call as plain text — suppressing this and all remaining text"
                                        );
                                        tool_call_as_text = true;
                                        continue;
                                    }
                                    if first_word {
                                        let ttft_ms = llm_request_start.elapsed().as_millis();
                                        tracing::info!(
                                            ttft_ms,
                                            "LLM time-to-first-token"
                                        );
                                        let time_s = stt_sender.current_time_s().await;
                                        msg_out_tx
                                            .send(MsgOut::Event { time_s, event: Event::FirstWord })
                                            .await?;
                                        first_word = false;
                                    }
                                    // Resolve any pending numeric: check if this chunk
                                    // starts with a digit (mid-number) or not (real boundary).
                                    if let Some(held) = pending_numeric.take() {
                                        let next_is_digit = chunk.chars().next().is_some_and(|c| c.is_ascii_digit());
                                        if next_is_digit {
                                            // Mid-number punctuation — rejoin into buffer
                                            buffer.push_str(&held);
                                        } else {
                                            // Real boundary — flush it
                                            tts_tx.send_text(&held).await?;
                                        }
                                    }
                                    buffer.push_str(&chunk);
                                    // Send when we have a word boundary (ends with space or sentence punctuation).
                                    // This ensures "don't" and "well-known" stay together.
                                    let last_char = buffer.chars().last();
                                    let maybe_mid_number = matches!(last_char, Some('.' | ','))
                                        && matches!(buffer.chars().rev().nth(1), Some('0'..='9'));
                                    if maybe_mid_number {
                                        // Stash — we need the next chunk to decide
                                        pending_numeric = Some(buffer.clone());
                                        buffer.clear();
                                    } else {
                                        let is_word_boundary = matches!(
                                            last_char,
                                            Some(' ' | '.' | '!' | '?' | ',' | '\n')
                                        );
                                        if is_word_boundary {
                                            tts_tx.send_text(&buffer).await?;
                                            buffer.clear();
                                        }
                                    }
                                }
                                crate::llm::LlmResponseItem::ToolCall { call, handle } => {
                                    tracing::info!(?call, "LLM made tool call");
                                    // Handle reset_asr internally - don't forward to client
                                    if call.tool_name == "reset_asr" {
                                        tracing::info!("Handling reset_asr tool call internally");
                                        // Signal the main loop to restart STT
                                        let _ = internal_cmd_tx.send(InternalCmd::RestartStt).await;
                                        // Respond to the LLM with success
                                        let _ = handle.send(serde_json::json!({
                                            "success": true,
                                            "message": "ASR has been reset. The next transcription may start mid-sentence or lack context from what the user just said."
                                        })).await;
                                    } else {
                                        msg_out_tx.send(MsgOut::ToolCall { call, handle }).await?;
                                    }
                                }
                                crate::llm::LlmResponseItem::Error(err) => {
                                    return Err(anyhow::anyhow!("LLM error: {err}"));
                                }
                            }
                        }
                        // Send any remaining buffered text (including stashed numeric)
                        if let Some(held) = pending_numeric.take() {
                            buffer.insert_str(0, &held);
                        }
                        if !buffer.is_empty() {
                            tts_tx.send_text(&buffer).await?;
                        }
                        let llm_total_ms = llm_request_start.elapsed().as_millis();
                        tracing::info!(
                            llm_total_ms,
                            "LLM stream complete"
                        );
                        tts_tx.send_end_of_stream().await?;
                        Ok::<(), anyhow::Error>(())
                    }
                };
                let tts_to_client = {
                    let tts_out_tx = tts_out_tx.clone();
                    let last_stop_s = last_stop_s.clone();
                    let llm = llm.clone();
                    async move {
                        use std::sync::atomic::Ordering;
                        let mut tts_rx = tts_rx;
                        let mut first_audio = true;
                        let mut message_count = 0u32;
                        // FIFO queue for text messages - text is sent after corresponding audio
                        let mut text_queue: std::collections::VecDeque<TtsOut> = std::collections::VecDeque::new();

                        // Wall clock time when TTS started - used to pace output.
                        // We send audio 300ms early to allow client jitter buffer to fill,
                        // which means up to 300ms of extra audio in flight on interruption.
                        let wall_start = std::time::Instant::now();
                        let wait_until = |target_s: f64| async move {
                            let elapsed = wall_start.elapsed().as_secs_f64();
                            if elapsed < target_s {
                                let wait_s = target_s - elapsed;
                                tokio::time::sleep(std::time::Duration::from_secs_f64(wait_s)).await;
                            }
                        };

                        let mut done = false;
                        while let Some(event) =
                            tts_rx.next_message(turn_idx).await.map_err(|e| {
                                tracing::error!(?e, "TTS receiver error");
                                e
                            })?
                        {
                            message_count += 1;
                            match event {
                                TtsOut::Audio { pcm, start_s, stop_s, turn_idx, .. } => {
                                    // Send audio 300ms early so client jitter buffer can fill
                                    wait_until(start_s - 0.3).await;

                                    if first_audio && stop_s > 0.0 {
                                        let time_s = stt_sender.current_time_s().await;
                                        msg_out_tx
                                            .send(MsgOut::Event {
                                                time_s,
                                                event: Event::FirstTtsAudio,
                                            })
                                            .await?;
                                        first_audio = false;
                                    }
                                    let adjusted_stop_s = start_time + stop_s;
                                    last_stop_s.store(adjusted_stop_s.to_bits(), Ordering::Release);

                                    // Check text queue BEFORE sending audio so we know if
                                    // this is the last packet (function call interruption).
                                    // Texts are still sent AFTER audio to preserve ordering.
                                    let mut texts_to_send = Vec::new();
                                    while let Some(TtsOut::Text {text, stop_s: text_stop_s, turn_idx: text_turn_idx, start_s }) = text_queue.pop_front() {
                                        if text_stop_s <= adjusted_stop_s {
                                            let has_pending = llm.read().await.has_pending_tool_results();
                                            done = done || has_pending && text.chars().any(|c| matches!(c, '.' | '!' | '?' | ';'));
                                            texts_to_send.push(TtsOut::Text {text, stop_s: text_stop_s, turn_idx: text_turn_idx, start_s });
                                        } else {
                                            text_queue.push_front(TtsOut::Text {text, stop_s: text_stop_s, turn_idx: text_turn_idx, start_s });
                                            break
                                        }
                                    }

                                    // Also check for user interruption
                                    done = done || user_interrupted.load(Ordering::Acquire);

                                    // Send audio — marked interrupted if this is the last packet
                                    tts_out_tx
                                        .send(Ok(TtsOut::Audio {
                                            pcm,
                                            start_s: start_time + start_s,
                                            stop_s: adjusted_stop_s,
                                            turn_idx,
                                            interrupted: done,
                                        }))
                                        .await?;

                                    // Send texts after audio
                                    for text in texts_to_send {
                                        tts_out_tx.send(Ok(text)).await?;
                                    }

                                    if done {
                                        break
                                    }
                                }
                                TtsOut::Text { text, start_s, stop_s, turn_idx } => {
                                    // Queue text instead of sending immediately
                                    // It will be sent after corresponding audio is sent
                                    text_queue.push_back(TtsOut::Text {
                                        text,
                                        start_s: start_time + start_s,
                                        stop_s: start_time + stop_s,
                                        turn_idx,
                                    });
                                }
                                TtsOut::TurnComplete { .. } => {
                                    // Shouldn't happen from TTS receiver
                                }
                            }
                        }
                        if !done {
                                    while let Some(TtsOut::Text {text, stop_s: text_stop_s, turn_idx: text_turn_idx, start_s }) = text_queue.pop_front() {
                                            tts_out_tx.send(Ok(TtsOut::Text {text, stop_s: text_stop_s, turn_idx: text_turn_idx, start_s })).await?
                                    }
                        }
                        tracing::info!(message_count, "TTS receiver finished");
                        let time_s = stt_sender.current_time_s().await;
                        msg_out_tx
                            .send(MsgOut::Event { time_s, event: Event::EndTtsAudio })
                            .await?;
                        Ok::<(), anyhow::Error>(())
                    }
                };
                tokio::try_join!(llm_to_tts, tts_to_client)?;
                let stop_s = f64::from_bits(last_stop_s.load(std::sync::atomic::Ordering::Acquire));
                Ok(stop_s)
            }
            .await;
            match &result {
                Ok(stop_s) => {
                    tracing::info!(?stop_s, "LLM/TTS task completed successfully");
                }
                Err(e) => {
                    tracing::error!(?e, "LLM/TTS task failed");
                }
            }
            match result {
                Ok(stop_s) => {
                    tts_out_tx
                        .send(Ok(TtsOut::TurnComplete { turn_idx, stop_s }))
                        .await?;
                }
                Err(e) => {
                    tts_out_tx.send(Err(e)).await?;
                }
            }
            Ok::<(), anyhow::Error>(())
        });
        Some(jh)
    }

    async fn send_event(&mut self, event: Event) -> Result<()> {
        let time_s = self.audio_time_s().await;
        self.msg_out_tx
            .send(MsgOut::Event { time_s, event })
            .await?;
        Ok(())
    }

    /// Handle end-of-turn signal from STT.
    ///
    /// `stt_time` is the STT-reported current_s converted to session time. This represents
    /// where STT is in processing the audio stream, which may lag behind audio_time_s().
    async fn on_end_of_turn(&mut self, stt_time: f64) -> Result<()> {
        let mut state = self.state.lock().await;
        // Avoid interruptions just after the end of turn.
        // Only flush if there are texts to process (user actually said something).
        if let State::Listening {
            since_s,
            texts,
            turn_idx,
        } = &mut *state
        {
            tracing::debug!(
                since_s,
                texts_len = texts.len(),
                turn_idx,
                stt_time,
                "on_end_of_turn in Listening"
            );
        }
        if let State::Listening {
            since_s,
            texts,
            turn_idx,
        } = &mut *state
        {
            if texts.is_empty() {
                if self.restart_stt_enabled
                    && self.min_inactivity_prob <= 0.5
                    && stt_time - *since_s > 1.0
                {
                    // Seen activity but no text - reset STT to recover from stuck silence.
                    tracing::info!(
                        min_inactivity_prob = self.min_inactivity_prob,
                        since_s = *since_s,
                        stt_time,
                        "restarting STT due to inactivity flicker with no text"
                    );
                    drop(state);
                    self.restart_stt().await?;
                }
                return Ok(());
            }
            if stt_time - *since_s <= 0.5 {
                return Ok(());
            }
            let flush_duration_s = self
                .session_config
                .lock()
                .await
                .as_ref()
                .map(|c| c.flush_duration_s)
                .unwrap_or(DEFAULT_FLUSH_FOR_S);
            let started_listening = *since_s;
            let turn_idx = *turn_idx;
            let texts = std::mem::take(texts);
            // Release lock before async operations
            drop(state);
            self.send_event(Event::Flushing {
                started_listening,
                text_chunks: texts.len(),
            })
            .await?;
            self.stt_sender.send_flush(flush_duration_s).await?;
            *self.state.lock().await = State::Flushing {
                since_s: stt_time,
                texts,
                flush_duration_s,
                turn_idx,
            };
        }
        Ok(())
    }

    /// Handle a step/tick from STT processing.
    ///
    /// `stt_time` is the STT-reported current_s converted to session time. This represents
    /// where STT is in processing the audio stream - useful for detecting pauses in speech.
    async fn on_step(
        &mut self,
        stt_time: f64,
        _end_of_turn: bool,
        inactivity_prob: f64,
    ) -> Result<()> {
        // VAD-based interruption: if we're generating/speaking and the VAD
        // detects likely voice activity (inactivity_prob < 0.4), signal
        // interruption immediately instead of waiting for STT text.
        if inactivity_prob < 0.4 {
            let state = self.state.lock().await;
            if let State::Processing { since_s, .. } = &*state {
                let elapsed = stt_time - since_s;
                if elapsed > 2.0 {
                    drop(state);
                    tracing::info!(
                        inactivity_prob,
                        elapsed,
                        "VAD-based interruption: voice activity detected while processing"
                    );
                    self.user_interrupted
                        .store(true, std::sync::atomic::Ordering::Release);
                }
            }
        }

        let jh = {
            let state = self.state.lock().await;
            // Note: Function call interruptions are now handled within the TTS task itself
            // by checking has_pending_tool_results() at punctuation boundaries
            if let State::Flushing {
                since_s,
                texts,
                flush_duration_s,
                turn_idx,
            } = &*state
                && stt_time - since_s > *flush_duration_s
            {
                let text = texts.join(" ");
                let turn_idx = *turn_idx;
                // Release lock before async operations
                drop(state);
                let _ = self.send_event(Event::EndOfTurn).await;
                self.llm_tts(&text, turn_idx).await
            } else if let State::Listening {
                since_s,
                texts,
                turn_idx,
            } = &*state
                && texts.is_empty()
            {
                // Check for pending tool results first - process them immediately
                if self.llm.read().await.has_pending_tool_results() {
                    tracing::info!("processing pending tool results");
                    let turn_idx = *turn_idx;
                    drop(state);
                    // Send empty message to trigger LLM with tool results only
                    // The prompt distinguishes "" (tool results) from "..." (silence)
                    self.llm_tts("", turn_idx).await
                } else {
                    let elapsed = stt_time - since_s;
                    let has_new = self.llm.read().await.has_new_tool_calls().await;
                    if elapsed > 0.05 && inactivity_prob > 0.8 && has_new {
                        tracing::info!(
                            elapsed_ms = ((stt_time - since_s) * 1000.0) as u32,
                            inactivity_prob,
                            "processing NEW tool calls after short delay"
                        );
                        let turn_idx = *turn_idx;
                        drop(state);
                        // Send empty message to trigger LLM with NEW tool calls
                        self.llm_tts("", turn_idx).await
                    } else if self.silence_prompts < 5 {
                        // Check for silence timeout - if user hasn't spoken for a while, send "..."
                        let silence_timeout_s = self
                            .session_config
                            .lock()
                            .await
                            .as_ref()
                            .map_or(5.0, |c| c.silence_timeout_s);
                        let elapsed = stt_time - since_s;
                        if silence_timeout_s > 0.0 && elapsed > silence_timeout_s {
                            self.silence_prompts += 1;
                            tracing::info!(
                                elapsed,
                                since_s,
                                stt_time,
                                silence_prompts = self.silence_prompts,
                                "silence timeout triggered"
                            );
                            let turn_idx = *turn_idx;
                            drop(state);
                            // Restart STT to clear accumulated silence/flush samples
                            self.restart_stt().await?;
                            self.llm_tts("...", turn_idx).await
                        } else {
                            None
                        }
                    } else {
                        None
                    }
                }
            } else {
                None
            }
        };
        if let Some(jh) = jh {
            let mut state = self.state.lock().await;
            let turn_idx = state.turn_idx();
            *state = State::Processing {
                since_s: stt_time,
                turn_idx,
                _jh: jh,
            };
        }
        Ok(())
    }

    /// Handle recognized text from STT.
    ///
    /// `stt_time` is the STT-reported start_s converted to session time. This represents
    /// when the speech actually started in the audio - more accurate than audio_time_s()
    /// for knowing when the user spoke, since STT has already analyzed the audio.
    async fn on_text(&mut self, text: String, stt_time: f64) -> Result<()> {
        tracing::info!(
            ?text,
            stt_time,
            silence_prompts = self.silence_prompts,
            "on_text called"
        );
        self.msg_out_tx
            .send(MsgOut::SttText {
                text: text.clone(),
                start_s: stt_time,
            })
            .await?;
        // User spoke - reset silence prompt counter
        self.silence_prompts = 0;
        self.min_inactivity_prob = 1.0;
        self.last_inactivity_prob = 1.0;
        let mut state = self.state.lock().await;
        match &mut *state {
            State::Flushing { texts, .. } | State::Listening { texts, .. } => {
                texts.push(text);
            }
            State::Processing { turn_idx, .. } => {
                let new_turn_idx = *turn_idx + 1;

                // Signal the LLM/TTS task to stop after its next audio packet.
                // It will mark that packet interrupted=true so the client fades out.
                self.user_interrupted
                    .store(true, std::sync::atomic::Ordering::Release);

                // Extract JoinHandle so the task isn't aborted — it needs to
                // send one more audio packet before exiting gracefully.
                let old_state = std::mem::replace(
                    &mut *state,
                    State::Listening {
                        since_s: stt_time,
                        texts: vec![text],
                        turn_idx: new_turn_idx,
                    },
                );
                drop(state);

                if let State::Processing { _jh, .. } = old_state {
                    self.interrupted_task_jh = Some(_jh);
                }

                self.send_event(Event::Interrupted).await?;
            }
        }
        Ok(())
    }
}

impl SttSender {
    async fn send_audio(&mut self, audio: &[f32]) -> Result<()> {
        let mut stt_sender = self.0.lock().await;
        stt_sender.samples_sent += audio.len() as u64;
        stt_sender.ss.send_audio(audio).await?;
        Ok(())
    }

    async fn send_flush(&mut self, flush_duration_s: f64) -> Result<()> {
        let mut stt_sender = self.0.lock().await;
        let flush = vec![0.0; (flush_duration_s * INPUT_SAMPLE_RATE as f64) as usize];
        stt_sender.flush_samples_sent += flush.len() as u64;
        stt_sender.ss.send_audio(&flush).await?;
        Ok(())
    }

    async fn current_time_s(&self) -> f64 {
        let stt_sender = self.0.lock().await;
        stt_sender.samples_sent as f64 / INPUT_SAMPLE_RATE as f64
    }
}

#[derive(Debug, Clone)]
pub struct SessionConfig {
    pub voice_id: Option<String>,
    pub instructions: Option<String>,
    pub language: crate::system_prompt::Lang,
    /// If true, the assistant will speak first when the session starts.
    pub assistant_speaks_first: bool,
    /// Seconds of silence after LLM finishes before sending "..." to prompt continuation.
    /// Set to 0 or negative to disable. Default is 3.0 seconds.
    pub silence_timeout_s: f64,
    /// Tool definitions for the LLM.
    pub tools: Vec<crate::llm::ToolDef>,
    /// Duration of silence (in seconds) to send to STT to flush the pipeline after
    /// the user stops speaking. Default is 0.5s.
    pub flush_duration_s: f64,
    /// Padding bonus for STT. Positive values make the model pad more (wait longer before
    /// finalizing), negative values make it pad less. Range: -4.0 to 4.0. Default is 0.0.
    pub padding_bonus: f64,
    /// TTS rewrite rules. Language codes like "en", "fr", "de", "es", "pt" enable all
    /// rewriting rules for that language. Passed via json_config to the TTS stream.
    pub rewrite_rules: Option<String>,
    /// Extra JSON config to merge into the STT stream's json_config.
    pub stt_extra_config: Option<String>,
    /// Extra JSON config to merge into the TTS stream's json_config.
    pub tts_extra_config: Option<String>,
    /// Extra JSON config to merge into the LLM chat completion request body.
    pub llm_extra_config: Option<String>,
}

pub enum MsgIn {
    Config(SessionConfig),
    Audio(Vec<u8>),
}

/// All outbound messages to the client.
pub enum MsgOut {
    /// Encoded audio data ready to send.
    /// When `interrupted` is true, the client should fade out over ~200ms instead of 10ms.
    Audio {
        data: Vec<u8>,
        start_s: f64,
        stop_s: f64,
        turn_idx: u64,
        interrupted: bool,
    },
    /// Text being spoken by the assistant (for captions/display).
    TtsText {
        text: String,
        start_s: f64,
        stop_s: f64,
        turn_idx: u64,
    },
    /// Transcription of user speech.
    SttText { text: String, start_s: f64 },
    /// Lifecycle event.
    Event { time_s: f64, event: Event },
    /// Tool call from the LLM. Client should execute the tool and send result via the handle.
    ToolCall {
        call: crate::llm::ToolCall,
        handle: crate::llm::ToolCallHandle,
    },
}

/// Handle for sending input to a voice session.
///
/// When this handle is dropped, the session will terminate normally.
pub struct SessionInputHandle {
    msg_in: tokio::sync::mpsc::Sender<MsgIn>,
    input_closed: std::sync::Arc<std::sync::atomic::AtomicBool>,
}

impl SessionInputHandle {
    /// Send encoded audio data to the session.
    ///
    /// The audio should be encoded in the format specified when starting the session.
    /// This method will block if the channel is full (backpressure).
    pub async fn send_audio(&self, data: Vec<u8>) -> Result<()> {
        self.msg_in.send(MsgIn::Audio(data)).await?;
        Ok(())
    }

    /// Send or update the session configuration.
    ///
    /// Must be called before sending audio if no `initial_config` was provided
    /// to [`start_session`].
    pub async fn send_config(&self, config: SessionConfig) -> Result<()> {
        self.msg_in.send(MsgIn::Config(config)).await?;
        Ok(())
    }
}

impl Drop for SessionInputHandle {
    fn drop(&mut self) {
        self.input_closed
            .store(true, std::sync::atomic::Ordering::Release);
    }
}

/// Handle for receiving output from a voice session.
///
/// Contains the output channel and the session task. Use [`receive`](Self::receive)
/// to get messages from the session.
pub struct SessionOutputHandle {
    msg_out: tokio::sync::mpsc::Receiver<MsgOut>,
    task: Option<tokio::task::JoinHandle<Result<()>>>,
    input_closed: std::sync::Arc<std::sync::atomic::AtomicBool>,
}

impl SessionOutputHandle {
    /// Receive the next outbound message from the session.
    ///
    /// Returns:
    /// - `Ok(Some(msg))` - a message is available
    /// - `Ok(None)` - session ended normally (input handle was dropped)
    /// - `Err(e)` - an error occurred (task error, panic, or unexpected closure)
    pub async fn receive(&mut self) -> Result<Option<MsgOut>> {
        match self.msg_out.recv().await {
            Some(msg) => {
                match &msg {
                    MsgOut::Audio {
                        start_s,
                        stop_s,
                        turn_idx,
                        interrupted,
                        data,
                    } => {
                        tracing::debug!(
                            start_s,
                            stop_s,
                            turn_idx,
                            interrupted,
                            bytes = data.len(),
                            "OUT Audio"
                        );
                    }
                    MsgOut::TtsText {
                        text,
                        start_s,
                        stop_s,
                        turn_idx,
                    } => {
                        tracing::debug!(start_s, stop_s, turn_idx, text, "OUT TtsText");
                    }
                    MsgOut::SttText { text, start_s } => {
                        tracing::debug!(start_s, text, "OUT SttText");
                    }
                    MsgOut::Event { time_s, event } => {
                        tracing::info!(time_s, ?event, "OUT Event");
                    }
                    MsgOut::ToolCall { call, .. } => {
                        tracing::info!(tool_name = call.tool_name, "OUT ToolCall");
                    }
                }
                Ok(Some(msg))
            }
            None => {
                // Output channel closed - determine why
                if let Some(task) = self.task.take() {
                    match task.await {
                        Ok(Ok(())) => {
                            // Task ended normally
                            if self.input_closed.load(std::sync::atomic::Ordering::Acquire) {
                                Ok(None)
                            } else {
                                Err(anyhow::anyhow!("unexpected closing of output"))
                            }
                        }
                        Ok(Err(e)) => Err(e),
                        Err(join_err) => Err(anyhow::anyhow!("task panicked: {join_err}")),
                    }
                } else {
                    // Task was already awaited, session ended normally
                    Ok(None)
                }
            }
        }
    }
}

/// Start a voice AI session.
///
/// Creates a new session and returns separate handles for input and output.
/// The session task is spawned internally.
///
/// # Channel Semantics
///
/// - **Normal termination**: When `SessionInputHandle` is dropped (client disconnects),
///   the session ends and `SessionOutputHandle::receive()` returns `Ok(None)`.
/// - **Internal error**: On any processing error, `SessionOutputHandle::receive()` returns
///   `Err(e)`.
///
/// # Arguments
///
/// * `tts_client` - Text-to-speech client for audio synthesis
/// * `stt_client` - Speech-to-text client for transcription
/// * `llm` - Language model for generating responses
/// * `initial_config` - Optional initial session configuration (can also be sent via input handle)
/// * `io_format` - Audio format pair for input decoding and output encoding
///
/// # Example
///
/// ```ignore
/// let io = IoFormat { input: decoder::Format::OggOpus, output: encoder::Format::OggOpus };
/// let (input, mut output) = start_session(
///     tts_client, stt_client, llm, Some(config), io
/// ).await?;
///
/// // Producer task sends audio
/// tokio::spawn(async move {
///     input.send_audio(audio_bytes).await.ok();
///     // input is dropped when done -> session ends normally
/// });
///
/// // Consumer loop receives messages
/// while let Some(msg) = output.receive().await? {
///     match msg {
///         MsgOut::Audio { data, .. } => { /* send audio */ }
///         MsgOut::TtsText { text, .. } => { /* send caption */ }
///         MsgOut::SttText { text, .. } => { /* send transcription */ }
///         MsgOut::Event { event, .. } => { /* handle event */ }
///     }
/// }
/// // Returns Ok(None) when session ends normally
/// ```
pub async fn start_session(
    tts_client: Arc<TtsClient>,
    stt_client: Arc<SttClient>,
    llm: Arc<crate::llm::Llm>,
    initial_config: Option<SessionConfig>,
    io_format: crate::IoFormat,
) -> Result<(SessionInputHandle, SessionOutputHandle)> {
    let (msg_in_tx, msg_in_rx) = tokio::sync::mpsc::channel::<MsgIn>(MSG_IN_CHANNEL_CAPACITY);
    let (msg_out_tx, msg_out_rx) = tokio::sync::mpsc::channel::<MsgOut>(MSG_OUT_CHANNEL_CAPACITY);

    let input_closed = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));

    let task = tokio::spawn(run(
        tts_client,
        stt_client,
        llm,
        initial_config,
        msg_out_tx,
        msg_in_rx,
        io_format,
    ));

    let input_handle = SessionInputHandle {
        msg_in: msg_in_tx,
        input_closed: input_closed.clone(),
    };
    let output_handle = SessionOutputHandle {
        msg_out: msg_out_rx,
        task: Some(task),
        input_closed,
    };

    Ok((input_handle, output_handle))
}

/// Internal: Run the voice AI multiplexing loop.
async fn run(
    tts_client: Arc<TtsClient>,
    stt_client: Arc<SttClient>,
    llm: Arc<crate::llm::Llm>,
    initial_config: Option<SessionConfig>,
    msg_out_tx: tokio::sync::mpsc::Sender<MsgOut>,
    mut msg_in_rx: tokio::sync::mpsc::Receiver<MsgIn>,
    io_format: crate::IoFormat,
) -> Result<()> {
    let input_format = io_format.input;
    let output_format = io_format.output;
    let (tts_out_tx, mut tts_out_rx) =
        tokio::sync::mpsc::channel::<Result<TtsOut>>(TTS_OUT_CHANNEL_CAPACITY);
    let assistant_speaks_first = initial_config
        .as_ref()
        .is_some_and(|c| c.assistant_speaks_first);
    let (mut session, sender) = Session::new(
        tts_client,
        stt_client,
        llm,
        tts_out_tx,
        msg_out_tx.clone(),
        initial_config,
    )
    .await?;
    // If assistant speaks first, trigger initial greeting
    if assistant_speaks_first && let Some(jh) = session.llm_tts("[start]", 0).await {
        *session.state.lock().await = State::Processing {
            since_s: 0.0,
            turn_idx: 0,
            _jh: jh,
        };
    }
    let session_config = session.session_config.clone();
    let out_send_loop = {
        let msg_out_tx = msg_out_tx.clone();
        let sender = sender.clone();
        let state = session.state.clone();
        let transmitted: Arc<Mutex<Vec<String>>> = session.llm.read().await.transmitted();
        async move {
            let mut encoder =
                crate::encoder::Encoder::new(output_format, OUTPUT_FRAME_SIZE, OUTPUT_SAMPLE_RATE)?;
            if let Some(header) = encoder.header() {
                msg_out_tx
                    .send(MsgOut::Audio {
                        data: header.to_vec(),
                        start_s: 0.0,
                        stop_s: 0.0,
                        turn_idx: 0,
                        interrupted: false,
                    })
                    .await?;
            }
            let mut last_encoder_turn_idx: u64 = 0;
            while let Some(msg) = tts_out_rx.recv().await {
                // Handle errors from the LLM/TTS task
                let msg = msg?;
                let current_turn_idx = state.lock().await.turn_idx();
                match msg {
                    TtsOut::Audio {
                        pcm,
                        start_s,
                        stop_s,
                        turn_idx,
                        interrupted,
                    } => {
                        if turn_idx < current_turn_idx {
                            tracing::debug!(
                                "[out_send] SKIP Audio: turn={turn_idx} < current={current_turn_idx} start_s={start_s:.3} stop_s={stop_s:.3}"
                            );
                            continue;
                        }
                        // Reset encoder on turn change to flush stale Opus state
                        if turn_idx != last_encoder_turn_idx {
                            tracing::debug!(
                                "[out_send] Turn changed {last_encoder_turn_idx} -> {turn_idx}, resetting encoder"
                            );
                            encoder = crate::encoder::Encoder::new(
                                output_format,
                                OUTPUT_FRAME_SIZE,
                                OUTPUT_SAMPLE_RATE,
                            )?;
                            if let Some(header) = encoder.header() {
                                msg_out_tx
                                    .send(MsgOut::Audio {
                                        data: header.to_vec(),
                                        start_s: 0.0,
                                        stop_s: 0.0,
                                        turn_idx,
                                        interrupted: false,
                                    })
                                    .await?;
                            }
                            last_encoder_turn_idx = turn_idx;
                        }
                        let encoded = encoder.encode(&pcm)?;
                        if !encoded.data.is_empty() {
                            tracing::debug!(
                                "[out_send] SEND Audio: turn={turn_idx} start_s={start_s:.3} stop_s={stop_s:.3} interrupted={interrupted} bytes={}",
                                encoded.data.len()
                            );
                            msg_out_tx
                                .send(MsgOut::Audio {
                                    data: encoded.data,
                                    start_s,
                                    stop_s,
                                    turn_idx,
                                    interrupted,
                                })
                                .await?;
                        }
                    }
                    TtsOut::Text {
                        text,
                        start_s,
                        stop_s,
                        turn_idx,
                    } => {
                        // Skip stale text from old turns
                        if turn_idx < current_turn_idx {
                            tracing::debug!(
                                "[out_send] SKIP Text: turn={turn_idx} < current={current_turn_idx} start_s={start_s:.3} stop_s={stop_s:.3} text={text:?}"
                            );
                            continue;
                        }
                        tracing::debug!(
                            "[out_send] SEND Text: turn={turn_idx} start_s={start_s:.3} stop_s={stop_s:.3} text={text:?}"
                        );
                        // Text was actually spoken - record it
                        transmitted.lock().await.push(text.clone());
                        msg_out_tx
                            .send(MsgOut::TtsText {
                                text,
                                start_s,
                                stop_s,
                                turn_idx,
                            })
                            .await?;
                    }
                    TtsOut::TurnComplete { turn_idx, stop_s } => {
                        // LLM/TTS task finished normally - transition to Listening
                        // Skip stale TurnComplete from interrupted turns
                        if turn_idx < current_turn_idx {
                            continue;
                        }
                        let mut state_guard = state.lock().await;
                        if let State::Processing {
                            turn_idx: state_turn_idx,
                            ..
                        } = &*state_guard
                        {
                            // Only transition if still processing the same turn
                            if *state_turn_idx == turn_idx {
                                let stt_time = sender.current_time_s().await;
                                // Use the later of stop_s and stt_time so that
                                // the silence timer starts from when the AI
                                // actually finished speaking, not from when the
                                // audio was logically scheduled.  When the LLM is
                                // slow, stop_s can lag behind stt_time by the LLM
                                // latency, which would cause the silence timeout
                                // to fire almost immediately.
                                let since_s = stop_s.max(stt_time);
                                let drift = stop_s - stt_time;
                                tracing::info!(
                                    stop_s,
                                    stt_time,
                                    since_s,
                                    drift,
                                    turn_idx,
                                    "transitioning to Listening"
                                );
                                *state_guard = State::Listening {
                                    since_s,
                                    texts: vec![],
                                    turn_idx: turn_idx + 1,
                                }
                            }
                        }
                    }
                }
            }
            Ok::<(), anyhow::Error>(())
        }
    };
    let internal_cmd_tx = session.internal_cmd_tx.clone();
    let tr_loop = async move { session.transcription_receive_loop().await };
    let recv_loop = async move {
        let mut sender = sender;
        let mut greeted = assistant_speaks_first; // true = already greeted at startup
        let mut decoder =
            crate::decoder::Decoder::new(input_format, INPUT_SAMPLE_RATE, INPUT_FRAME_SIZE)?;
        while let Some(msg) = msg_in_rx.recv().await {
            match msg {
                MsgIn::Config(config) => {
                    let should_greet = !greeted && config.assistant_speaks_first;
                    tracing::info!(?config, should_greet, "received session configuration");
                    *session_config.lock().await = Some(config);
                    if should_greet {
                        greeted = true;
                        let _ = internal_cmd_tx.send(InternalCmd::Greet).await;
                    }
                }
                MsgIn::Audio(audio) => {
                    if session_config.lock().await.is_none() {
                        return Err(anyhow::anyhow!(
                            "Session configuration required before sending audio"
                        ));
                    }
                    let audio = decoder.decode(&audio)?;
                    sender.send_audio(&audio).await?
                }
            }
        }
        Ok::<(), anyhow::Error>(())
    };
    tokio::pin!(out_send_loop);
    tokio::pin!(tr_loop);
    tokio::pin!(recv_loop);

    tokio::select! {
        res = &mut out_send_loop => res.context("TTS/LLM output loop failed"),
        res = &mut tr_loop => {
            // STT stream ending should not kill the session — the LLM/TTS
            // pipeline may still be producing output (e.g. TTS-only sessions,
            // or slow LLM responses during which STT times out).
            match &res {
                Ok(()) => tracing::info!("STT transcription loop ended normally, session continues"),
                Err(e) => tracing::warn!(?e, "STT transcription loop failed, session continues"),
            }
            // Wait for the output loop or recv loop to finish instead
            tokio::select! {
                res = &mut out_send_loop => res.context("TTS/LLM output loop failed"),
                res = &mut recv_loop => res.context("audio input loop failed"),
            }
        },
        res = &mut recv_loop => res.context("audio input loop failed"),
    }
}
