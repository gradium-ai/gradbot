//! OpenAI Realtime API compatible server implementation.
//!
//! This module demonstrates using the `GradbotClients` API which creates clients
//! once at startup and reuses them across sessions. For the simpler `run()` API
//! which creates clients per-session, see `twilio_server.rs`.

use crate::{Config, openai_protocol};
use anyhow::Result;
use axum::extract::ws;
use futures::StreamExt;
use gradbot::{
    Event, Lang, Llm, MsgOut, SessionConfig, SessionInputHandle, SessionOutputHandle, SttClient,
    ToolCallHandle, TtsClient,
};
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::Mutex;

/// Pending tool calls waiting for results from the client
type PendingToolCalls = Arc<Mutex<HashMap<String, ToolCallHandle>>>;

pub struct State {
    tts_client: Arc<TtsClient>,
    stt_client: Arc<SttClient>,
    llm: Arc<Llm>,
    config: Arc<Config>,
    cnt: std::sync::atomic::AtomicU64,
    trace_cnt: std::sync::atomic::AtomicU64,
}

#[derive(Debug, serde::Deserialize, serde::Serialize)]
struct TimedEvent {
    wall_time_s: f64,
    time_s: f64,
    #[serde(flatten)]
    event: Event,
}

pub type WebSocketSender = futures::stream::SplitSink<ws::WebSocket, ws::Message>;
pub type WebSocketReceiver = futures_util::stream::SplitStream<ws::WebSocket>;

/// Consumer loop that handles MsgOut messages and sends to the WebSocket/log.
async fn msg_out_consumer(
    mut ws: WebSocketSender,
    mut output: SessionOutputHandle,
    start_time: timens::Time,
    mut log: Option<tokio::fs::File>,
    pending_tool_calls: PendingToolCalls,
) -> Result<()> {
    use futures::SinkExt;
    use openai_protocol::ServerEvent;

    while let Some(msg) = output.receive().await? {
        match msg {
            MsgOut::Audio { data, .. } => {
                let event = ServerEvent::response_audio_delta(data);
                let msg = serde_json::to_string(&event)?;
                ws.send(ws::Message::Text(msg.into())).await?;
            }
            MsgOut::TtsText { text, .. } => {
                let event = ServerEvent::response_text_delta(text);
                let msg = serde_json::to_string(&event)?;
                ws.send(ws::Message::Text(msg.into())).await?;
            }
            MsgOut::SttText { text, start_s: _ } => {
                let event =
                    ServerEvent::conversation_item_input_audio_transcription_delta(text, 0.);
                let msg = serde_json::to_string(&event)?;
                ws.send(ws::Message::Text(msg.into())).await?;
            }
            MsgOut::Event { time_s, event } => {
                if let Some(log) = &mut log {
                    use tokio::io::AsyncWriteExt;
                    let wall_time_s = (timens::Time::now() - start_time).to_sec();
                    let timed_event = TimedEvent {
                        wall_time_s,
                        time_s,
                        event,
                    };
                    let log_line = serde_json::to_string(&timed_event)? + "\n";
                    log.write_all(log_line.as_bytes()).await?;
                }
            }
            MsgOut::ToolCall { call, handle } => {
                tracing::info!(call_id = %call.call_id, tool = %call.tool_name, "Sending tool call to client");
                // Store handle for later when client sends result
                pending_tool_calls
                    .lock()
                    .await
                    .insert(call.call_id.clone(), handle);
                // Send tool call to client
                let event = ServerEvent::unmute_response_function_call(
                    call.call_id,
                    call.tool_name,
                    call.args,
                );
                let msg = serde_json::to_string(&event)?;
                ws.send(ws::Message::Text(msg.into())).await?;
            }
        }
    }
    Ok(())
}

/// Builds the `gradbot` `SessionConfig` from a client's `session.update`
/// payload. Pulled out of `msg_in_producer` so the wire-to-library mapping is
/// unit-testable without a real WebSocket connection.
fn build_gradbot_session_config(session: openai_protocol::SessionConfig) -> SessionConfig {
    let endpointing = session.resolve_endpointing();
    let instructions = session.instructions;
    let language = session
        .lang
        .as_deref()
        .and_then(|s| serde_json::from_value(serde_json::Value::String(s.to_string())).ok())
        .unwrap_or(Lang::En);
    SessionConfig {
        voice_id: session.voice_id,
        instructions,
        language,
        // BENCHMARK TRAP: hardcoded, and there is no wire field to turn it
        // off. `gradbot-bench` sends no `session.update` today, so it never
        // reaches this line and never gets a greeting. The moment a sweep
        // task sends one to reach the endpointing knobs below, the server
        // will greet on `[start]` and that greeting becomes the first agent
        // audio the harness sees — recorded as turn 0's `FirstAgentAudio`,
        // with a latency that measures the greeting, not any answer. Turn 0
        // of *every* repetition is poisoned, silently and plausibly. Before
        // sending a `session.update` from the benchmark, either make this
        // configurable from the wire and set it false, or discard turn 0
        // explicitly in the report.
        assistant_speaks_first: true,
        silence_timeout_s: endpointing.silence_timeout_s,
        tools: vec![],
        flush_duration_s: endpointing.flush_duration_s,
        padding_bonus: endpointing.padding_bonus,
        rewrite_rules: None,
        stt_extra_config: None,
        tts_extra_config: None,
        llm_extra_config: session.llm_extra_config,
        min_listen_before_flush_s: endpointing.min_listen_before_flush_s,
        vad_eot_threshold: endpointing.vad_eot_threshold,
    }
}

/// Producer loop that reads from WebSocket and sends to the session.
async fn msg_in_producer(
    mut ws: WebSocketReceiver,
    input: SessionInputHandle,
    pending_tool_calls: PendingToolCalls,
) -> Result<()> {
    use axum::extract::ws::Message;
    use futures_util::StreamExt;
    use openai_protocol::ClientEvent;

    while let Some(msg) = ws.next().await {
        let msg: ClientEvent = match msg? {
            Message::Text(t) => serde_json::from_str(&t)?,
            Message::Binary(b) => serde_json::from_slice(&b)?,
            Message::Close(cf) => {
                tracing::info!(?cf, "activity websocket closed by client");
                return Ok(());
            }
            Message::Ping(_) | Message::Pong(_) => continue,
        };
        match msg {
            ClientEvent::UnmuteInputAudioBufferAppendAnonymized { .. } => {}
            ClientEvent::SessionUpdate {
                session,
                event_id: _,
            } => {
                tracing::info!(?session, "session update");
                input
                    .send_config(build_gradbot_session_config(session))
                    .await?;
            }
            ClientEvent::InputAudioBufferAppend { audio, event_id: _ } => {
                input.send_audio(audio).await?;
            }
            ClientEvent::UnmuteFunctionCallResult {
                call_id,
                result,
                is_error,
                event_id: _,
            } => {
                tracing::info!(%call_id, %is_error, "Received function call result from client");
                if let Some(handle) = pending_tool_calls.lock().await.remove(&call_id) {
                    if is_error {
                        let error_msg = result.as_str().unwrap_or("Unknown error").to_string();
                        if let Err(e) = handle.send_error(anyhow::anyhow!("{}", error_msg)).await {
                            tracing::warn!(%call_id, ?e, "Failed to send error result");
                        }
                    } else if let Err(e) = handle.send(result).await {
                        tracing::warn!(%call_id, ?e, "Failed to send result");
                    }
                } else {
                    tracing::warn!(%call_id, "Received result for unknown tool call");
                }
            }
        }
    }
    Ok(())
}

pub async fn realtime(
    ws: axum::extract::ws::WebSocketUpgrade,
    state: axum::extract::State<Arc<State>>,
) -> axum::response::Response {
    async fn websocket(s: axum::extract::ws::WebSocket, state: Arc<State>) {
        let (tx, rx) = s.split();
        let start_time = timens::Time::now();
        let log_file = if state.config.log_sessions {
            let ts = start_time.to_int_ns_since_epoch();
            let cnt = state.cnt.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            let log_file = std::path::PathBuf::from(&state.config.log_dir)
                .join(format!("session_{ts:10}_{cnt:06}.jsonl"));
            let log_file = tokio::fs::File::create(log_file).await.ok();
            tracing::info!(?log_file, "logging session");
            log_file
        } else {
            None
        };

        let tracer = match &state.config.trace_dir {
            None => gradbot::Tracer::disabled(),
            Some(dir) => {
                let ts = start_time.to_int_ns_since_epoch();
                let cnt = state
                    .trace_cnt
                    .fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                let path = std::path::PathBuf::from(dir).join(format!("trace_{ts}_{cnt:06}.jsonl"));
                match gradbot::Tracer::to_file(&path) {
                    Ok(t) => {
                        tracing::info!(path = %path.display(), "latency tracing enabled");
                        t
                    }
                    Err(e) => {
                        tracing::warn!(?e, "failed to open trace file, continuing untraced");
                        gradbot::Tracer::disabled()
                    }
                }
            }
        };

        let (input, output) = match gradbot::start_session(
            state.tts_client.clone(),
            state.stt_client.clone(),
            state.llm.clone(),
            None, // Session config will be sent via message
            gradbot::IoFormat {
                input: gradbot::decoder::Format::pcm(24000),
                output: gradbot::encoder::Format::OggOpus,
            },
            tracer,
        )
        .await
        {
            Ok(v) => v,
            Err(err) => {
                tracing::error!(?err, "failed to start session");
                return;
            }
        };

        // Shared state for pending tool calls
        let pending_tool_calls: PendingToolCalls = Arc::new(Mutex::new(HashMap::new()));

        let consumer =
            msg_out_consumer(tx, output, start_time, log_file, pending_tool_calls.clone());
        let producer = msg_in_producer(rx, input, pending_tool_calls);

        tokio::select! {
            res = consumer => {
                if let Err(err) = res {
                    tracing::error!(?err, "consumer error");
                }
            }
            res = producer => {
                if let Err(err) = res {
                    tracing::error!(?err, "producer error");
                }
            }
        }
    }

    tracing::info!("realtime websocket connection");
    let state = state.0;
    ws.protocols(["realtime"])
        .on_upgrade(move |v| websocket(v, state))
}

pub async fn serve(config: Config) -> Result<()> {
    use std::str::FromStr;
    let tts_client = TtsClient::new(Some(&config.gradium_api_key), &config.gradium_base_url)?;
    let stt_client = SttClient::new(Some(&config.gradium_api_key), &config.gradium_base_url)?;
    let llm = Llm::new(
        config.llm_base_url.clone(),
        config.max_completion_tokens.unwrap_or(4096),
        None,
        None,
    )
    .await?;
    let config = Arc::new(config);
    let state = State {
        tts_client: Arc::new(tts_client),
        stt_client: Arc::new(stt_client),
        llm: Arc::new(llm),
        config: config.clone(),
        cnt: std::sync::atomic::AtomicU64::new(0),
        trace_cnt: std::sync::atomic::AtomicU64::new(0),
    };
    let state = std::sync::Arc::new(state);
    let app = axum::Router::new().route("/v1/realtime", axum::routing::get(realtime));

    let trace_layer =
        tower::ServiceBuilder::new().layer(tower_http::trace::TraceLayer::new_for_http());

    let app = match config.static_dir.as_ref() {
        Some(static_dir) => app
            .fallback_service(
                tower_http::services::ServeDir::new(static_dir)
                    .append_index_html_on_directories(true),
            )
            .layer(trace_layer)
            .with_state(state),
        None => app.layer(trace_layer).with_state(state),
    };
    let sock_addr = std::net::SocketAddr::from((
        std::net::IpAddr::from_str(config.addr.as_str())
            .unwrap_or(std::net::IpAddr::V6(std::net::Ipv6Addr::LOCALHOST)),
        config.port,
    ));
    tracing::info!("starting server on {sock_addr}");
    let socket = match sock_addr {
        std::net::SocketAddr::V4(_) => tokio::net::TcpSocket::new_v4()?,
        std::net::SocketAddr::V6(_) => tokio::net::TcpSocket::new_v6()?,
    };
    socket.set_reuseaddr(true)?;
    socket.bind(sock_addr)?;
    let listener = socket.listen(1024)?;
    let app = app.into_make_service_with_connect_info::<std::net::SocketAddr>();
    if let Err(err) = axum::serve(listener, app).await {
        tracing::error!(?err, "axum server error");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn wire_session(llm_extra_config: Option<String>) -> openai_protocol::SessionConfig {
        openai_protocol::SessionConfig {
            instructions: None,
            voice: None,
            voice_id: None,
            allow_recording: false,
            lang: None,
            flush_duration_s: None,
            min_listen_before_flush_s: None,
            vad_eot_threshold: None,
            padding_bonus: None,
            silence_timeout_s: None,
            llm_extra_config,
        }
    }

    /// Regression guard: a session update omitting `llm_extra_config` must
    /// still produce a `gradbot::SessionConfig` with `llm_extra_config:
    /// None` -- exactly what this server has always hardcoded. Every
    /// existing consumer (demos, `gradbot_py`, `examples/gradbot-client.rs`)
    /// sends no such field.
    #[test]
    fn session_update_without_llm_extra_config_yields_none() {
        let config = build_gradbot_session_config(wire_session(None));
        assert_eq!(config.llm_extra_config, None);
    }

    /// An `llm_extra_config` set on the wire payload must be carried through
    /// unchanged to the constructed `gradbot::SessionConfig`.
    #[test]
    fn session_update_llm_extra_config_is_carried_through() {
        let config = build_gradbot_session_config(wire_session(Some(
            r#"{"tool_choice":"none"}"#.to_string(),
        )));
        assert_eq!(
            config.llm_extra_config,
            Some(r#"{"tool_choice":"none"}"#.to_string())
        );
    }
}
