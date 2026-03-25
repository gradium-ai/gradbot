//! Twilio Media Streams server implementation.
//!
//! This module demonstrates using the `gradbot::run()` API which creates
//! clients and starts a session in a single call. For the `GradbotClients` API
//! which allows reusing clients across sessions, see `openai_server.rs`.

use crate::{Config, TwilioConfig, twilio_protocol as prot};
use anyhow::Result;
use axum::extract::ws;
use base64::Engine;
use base64::engine::general_purpose::STANDARD as B64;
use gradbot::{MsgOut, SessionInputHandle, SessionOutputHandle};
use std::sync::Arc;

const DEFAULT_VOICE_ID: &str = "X8-_I8yFvYONny54";

pub struct State {
    config: Arc<Config>,
    session_config: gradbot::SessionConfig,
    cnt: std::sync::atomic::AtomicU64,
}

#[derive(Debug, serde::Deserialize, serde::Serialize)]
struct TimedEvent {
    wall_time_s: f64,
    time_s: f64,
    #[serde(flatten)]
    event: gradbot::Event,
}

pub type WebSocketSender = futures::stream::SplitSink<ws::WebSocket, ws::Message>;
pub type WebSocketReceiver = futures_util::stream::SplitStream<ws::WebSocket>;

/// Consumer loop that handles MsgOut messages and sends to the WebSocket/log.
async fn msg_out_consumer(
    mut ws: WebSocketSender,
    mut output: SessionOutputHandle,
    stream_sid: String,
    start_time: timens::Time,
    mut log: Option<tokio::fs::File>,
) -> Result<()> {
    use futures::SinkExt;

    while let Some(msg) = output.receive().await? {
        match msg {
            MsgOut::Audio { data, .. } => {
                let audio_b64 = B64.encode(&data);
                let event = prot::OutboundEvent::Media {
                    stream_sid: stream_sid.clone(),
                    media: prot::OutboundMedia { payload: audio_b64 },
                };
                let msg = serde_json::to_string(&event)?;
                ws.send(ws::Message::Text(msg.into())).await?;
            }
            MsgOut::TtsText { .. } | MsgOut::SttText { .. } => {
                // Twilio doesn't support text messages
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
                // Twilio doesn't support tool calls
                tracing::warn!(?call, "Tool call not supported in Twilio server");
                drop(handle); // Will mark as LOST
            }
        }
    }
    Ok(())
}

/// Producer loop that reads from WebSocket and sends to the session.
async fn msg_in_producer(mut ws: WebSocketReceiver, input: SessionInputHandle) -> Result<()> {
    use axum::extract::ws::Message;
    use futures_util::StreamExt;
    use prot::InboundEvent as Event;

    while let Some(msg) = ws.next().await {
        let msg = match msg? {
            Message::Text(t) => serde_json::from_str::<Event>(&t),
            Message::Binary(b) => serde_json::from_slice(&b),
            Message::Close(cf) => {
                tracing::info!(?cf, "activity websocket closed by client");
                return Ok(());
            }
            Message::Ping(_) | Message::Pong(_) => continue,
        };
        let msg = match msg {
            Ok(v) => v,
            Err(ref err) => {
                tracing::error!(?err, ?msg, "failed to parse inbound websocket message");
                continue;
            }
        };

        match msg {
            Event::Media {
                stream_sid: _,
                sequence_number: _,
                media,
            } => {
                let audio = B64.decode(&media.payload)?;
                input.send_audio(audio).await?;
            }
            Event::Stop { .. } => {
                tracing::info!("client stop");
                return Ok(());
            }
            Event::Connected { .. } => {
                tracing::info!("client connected");
            }
            Event::Dtmf { .. } => {
                tracing::info!("dtmf received");
            }
            Event::Mark { .. } => {
                tracing::info!("mark received");
            }
            Event::Start {
                stream_sid,
                sequence_number: _,
                start,
            } => {
                tracing::info!(?stream_sid, ?start, "stream started");
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
        use futures::StreamExt;

        let (tx, mut rx) = s.split();

        // Get stream ID first
        let stream_sid = loop {
            if let Some(msg) = rx.next().await {
                let msg = match msg {
                    Ok(ws::Message::Text(t)) => serde_json::from_str::<prot::InboundEvent>(&t),
                    Ok(ws::Message::Binary(b)) => serde_json::from_slice(&b),
                    Ok(ws::Message::Close(cf)) => {
                        tracing::info!(?cf, "activity websocket closed by client");
                        return;
                    }
                    Ok(ws::Message::Ping(_) | ws::Message::Pong(_)) => continue,
                    Err(err) => {
                        tracing::error!(?err, "websocket error");
                        return;
                    }
                };
                if let Ok(prot::InboundEvent::Start {
                    stream_sid,
                    sequence_number: _,
                    start: _,
                }) = msg
                {
                    tracing::info!(?stream_sid, "stream started");
                    break stream_sid;
                }
            } else {
                tracing::error!("no start message received");
                return;
            }
        };

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

        // Using gradbot::run() - creates clients and starts session in one call.
        // Model listing is cached globally, so subsequent calls are fast.
        let (input, output) = match gradbot::run(
            Some(&state.config.gradium_api_key),
            Some(&state.config.gradium_base_url),
            state.config.llm_base_url.as_deref(),
            None, // model auto-detected or from LLM_MODEL env var
            None, // API key from env vars
            state.config.max_completion_tokens,
            Some(state.session_config.clone()),
            gradbot::IoFormat {
                input: gradbot::decoder::Format::ulaw(8000),
                output: gradbot::encoder::Format::ulaw(8000),
            },
        )
        .await
        {
            Ok(v) => v,
            Err(err) => {
                tracing::error!(?err, "failed to start session");
                return;
            }
        };
        let consumer = msg_out_consumer(tx, output, stream_sid, start_time, log_file);
        let producer = msg_in_producer(rx, input);

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

async fn twiml(
    headers: axum::http::HeaderMap,
) -> std::result::Result<impl axum::response::IntoResponse, axum::http::StatusCode> {
    let host = headers
        .get("host")
        .and_then(|h| h.to_str().ok())
        .unwrap_or("example.com");

    // Validate host to prevent header injection into XML
    let is_valid_host = host
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | ':'));
    if !is_valid_host {
        return Err(axum::http::StatusCode::BAD_REQUEST);
    }

    tracing::info!(?host, "generating twiml");

    let twiml = format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://{host}/twilio-media" />
  </Connect>
</Response>"#
    );

    axum::response::Response::builder()
        .status(200)
        .header("Content-Type", "text/xml")
        .body(twiml)
        .map_err(|_| axum::http::StatusCode::INTERNAL_SERVER_ERROR)
}

pub async fn serve(config: Config, twilio_config: TwilioConfig) -> Result<()> {
    use std::str::FromStr;

    let voice_id = twilio_config
        .voice_id
        .clone()
        .unwrap_or_else(|| DEFAULT_VOICE_ID.to_string());
    let session_config = gradbot::SessionConfig {
        voice_id: Some(voice_id),
        instructions: Some(twilio_config.system_prompt.clone()),
        language: twilio_config.language,
        assistant_speaks_first: true,
        silence_timeout_s: 5.0,
        tools: vec![],
        flush_duration_s: gradbot::DEFAULT_FLUSH_FOR_S,
        padding_bonus: 0.0,
        rewrite_rules: None,
        stt_extra_config: None,
        tts_extra_config: None,
        llm_extra_config: None,
    };
    let config = Arc::new(config);
    let state = State {
        config: config.clone(),
        session_config,
        cnt: std::sync::atomic::AtomicU64::new(0),
    };
    let state = std::sync::Arc::new(state);
    let app = axum::Router::new()
        .route("/twiml", axum::routing::post(twiml))
        .route("/twilio-media", axum::routing::get(realtime));

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
