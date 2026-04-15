use crate::protocol::{ClientMessage, ServerMessage, SessionConfigWire, merge_with_pinned};
use anyhow::Result;
use axum::extract::ws;
use futures::StreamExt;
use gradbot::{
    Llm, MsgOut, SessionInputHandle, SessionOutputHandle, SttClient, ToolCallHandle, TtsClient,
};
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::Mutex;

type PendingToolCalls = Arc<Mutex<HashMap<String, ToolCallHandle>>>;

pub struct AppState {
    pub llm: Arc<Llm>,
    pub gradium_base_url: String,
    pub pinned: SessionConfigWire,
    pub log_sessions: bool,
    pub log_dir: String,
    pub cnt: std::sync::atomic::AtomicU64,
}

pub type WebSocketSender = futures::stream::SplitSink<ws::WebSocket, ws::Message>;
pub type WebSocketReceiver = futures_util::stream::SplitStream<ws::WebSocket>;

/// Extract Bearer token from the WebSocket upgrade request.
fn extract_bearer_token(headers: &axum::http::HeaderMap) -> Option<String> {
    headers
        .get(axum::http::header::AUTHORIZATION)?
        .to_str()
        .ok()?
        .strip_prefix("Bearer ")
        .map(|s| s.to_string())
}

pub async fn ws_handler(
    ws: ws::WebSocketUpgrade,
    headers: axum::http::HeaderMap,
    state: axum::extract::State<Arc<AppState>>,
) -> axum::response::Response {
    let client_api_key = extract_bearer_token(&headers);
    ws.on_upgrade(move |socket| handle_connection(socket, client_api_key, state.0))
}

async fn handle_connection(
    socket: ws::WebSocket,
    client_api_key: Option<String>,
    state: Arc<AppState>,
) {
    if let Err(err) = handle_connection_inner(socket, client_api_key, state).await {
        tracing::error!(?err, "connection handler error");
    }
}

async fn handle_connection_inner(
    socket: ws::WebSocket,
    client_api_key: Option<String>,
    state: Arc<AppState>,
) -> Result<()> {
    let api_key = client_api_key.as_deref();

    // Create per-session STT/TTS clients with the client's API key
    let tts = Arc::new(TtsClient::new(api_key, &state.gradium_base_url)?);
    let stt = Arc::new(SttClient::new(api_key, &state.gradium_base_url)?);

    // Start session — no initial config, wait for client's session.config message
    let io_format = gradbot::IoFormat {
        input: gradbot::decoder::Format::OggOpus,
        output: gradbot::encoder::Format::OggOpus,
    };
    let (input, output) =
        gradbot::start_session(tts, stt, state.llm.clone(), None, io_format).await?;

    let (ws_tx, ws_rx) = socket.split();
    let pending_tool_calls: PendingToolCalls = Arc::new(Mutex::new(HashMap::new()));

    // Wrap ws_tx in Arc<Mutex> so both loops can send
    let ws_tx = Arc::new(Mutex::new(ws_tx));

    tokio::select! {
        res = msg_out_consumer(ws_tx.clone(), output, pending_tool_calls.clone(), &state) => {
            if let Err(err) = res {
                tracing::error!(?err, "consumer error");
            }
        }
        res = msg_in_producer(ws_rx, input, pending_tool_calls, ws_tx, &state.pinned) => {
            if let Err(err) = res {
                tracing::error!(?err, "producer error");
            }
        }
    }

    Ok(())
}

/// Consumer loop: reads MsgOut from gradbot, sends to WebSocket client.
async fn msg_out_consumer(
    ws_tx: Arc<Mutex<WebSocketSender>>,
    mut output: SessionOutputHandle,
    pending_tool_calls: PendingToolCalls,
    state: &AppState,
) -> Result<()> {
    use futures::SinkExt;

    let start_time = std::time::Instant::now();
    let mut log_file = if state.log_sessions {
        let cnt = state.cnt.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let path =
            std::path::PathBuf::from(&state.log_dir).join(format!("session_{ts}_{cnt:06}.jsonl"));
        tokio::fs::File::create(path).await.ok()
    } else {
        None
    };

    while let Some(msg) = output.receive().await? {
        match msg {
            MsgOut::Audio {
                data,
                start_s,
                stop_s,
                turn_idx,
                interrupted,
            } => {
                let json_msg = ServerMessage::Audio {
                    start_s,
                    stop_s,
                    turn_idx,
                    interrupted,
                };
                let json = serde_json::to_string(&json_msg)?;
                let mut tx = ws_tx.lock().await;
                tx.send(ws::Message::Text(json.into())).await?;
                tx.send(ws::Message::Binary(data.into())).await?;
            }
            MsgOut::TtsText {
                text,
                start_s,
                stop_s,
                turn_idx,
            } => {
                let json_msg = ServerMessage::TtsText {
                    text,
                    start_s,
                    stop_s,
                    turn_idx,
                };
                let json = serde_json::to_string(&json_msg)?;
                ws_tx
                    .lock()
                    .await
                    .send(ws::Message::Text(json.into()))
                    .await?;
            }
            MsgOut::SttText { text, start_s } => {
                let json_msg = ServerMessage::SttText { text, start_s };
                let json = serde_json::to_string(&json_msg)?;
                ws_tx
                    .lock()
                    .await
                    .send(ws::Message::Text(json.into()))
                    .await?;
            }
            MsgOut::Event { time_s, event } => {
                let event_str = serde_json::to_string(&event)?;

                // Log events
                if let Some(log) = &mut log_file {
                    use tokio::io::AsyncWriteExt;
                    let wall_time_s = start_time.elapsed().as_secs_f64();
                    let log_line = serde_json::json!({
                        "wall_time_s": wall_time_s,
                        "time_s": time_s,
                        "event": event,
                    });
                    let line = serde_json::to_string(&log_line)? + "\n";
                    log.write_all(line.as_bytes()).await?;
                }

                let json_msg = ServerMessage::Event {
                    time_s,
                    event: event_str,
                };
                let json = serde_json::to_string(&json_msg)?;
                ws_tx
                    .lock()
                    .await
                    .send(ws::Message::Text(json.into()))
                    .await?;
            }
            MsgOut::ToolCall { call, handle } => {
                tracing::info!(call_id = %call.call_id, tool = %call.tool_name, "sending tool call to client");
                pending_tool_calls
                    .lock()
                    .await
                    .insert(call.call_id.clone(), handle);
                let json_msg = ServerMessage::ToolCall {
                    call_id: call.call_id,
                    tool_name: call.tool_name,
                    args: call.args,
                };
                let json = serde_json::to_string(&json_msg)?;
                ws_tx
                    .lock()
                    .await
                    .send(ws::Message::Text(json.into()))
                    .await?;
            }
        }
    }
    Ok(())
}

/// Producer loop: reads from WebSocket client, dispatches to gradbot session.
async fn msg_in_producer(
    mut ws_rx: WebSocketReceiver,
    input: SessionInputHandle,
    pending_tool_calls: PendingToolCalls,
    ws_tx: Arc<Mutex<WebSocketSender>>,
    pinned: &SessionConfigWire,
) -> Result<()> {
    use futures::SinkExt;
    use futures_util::StreamExt;

    while let Some(msg) = ws_rx.next().await {
        let msg = match msg? {
            ws::Message::Text(t) => {
                let client_msg: ClientMessage = serde_json::from_str(&t)?;
                match client_msg {
                    ClientMessage::SessionConfig { config } => {
                        let (merged, pinned_fields) = merge_with_pinned(config, pinned);
                        input.send_config(merged).await?;
                        let reply = ServerMessage::SessionConfigApplied { pinned_fields };
                        let json = serde_json::to_string(&reply)?;
                        ws_tx
                            .lock()
                            .await
                            .send(ws::Message::Text(json.into()))
                            .await?;
                    }
                    ClientMessage::ToolCallResult {
                        call_id,
                        result,
                        is_error,
                    } => {
                        tracing::info!(%call_id, %is_error, "received tool call result from client");
                        if let Some(handle) = pending_tool_calls.lock().await.remove(&call_id) {
                            if is_error {
                                let error_msg =
                                    result.as_str().unwrap_or("Unknown error").to_string();
                                if let Err(e) =
                                    handle.send_error(anyhow::anyhow!("{}", error_msg)).await
                                {
                                    tracing::warn!(%call_id, ?e, "failed to send error result");
                                }
                            } else if let Err(e) = handle.send(result).await {
                                tracing::warn!(%call_id, ?e, "failed to send result");
                            }
                        } else {
                            tracing::warn!(%call_id, "received result for unknown tool call");
                        }
                    }
                }
                continue;
            }
            ws::Message::Binary(b) => b,
            ws::Message::Close(cf) => {
                tracing::info!(?cf, "WebSocket closed by client");
                return Ok(());
            }
            ws::Message::Ping(_) | ws::Message::Pong(_) => continue,
        };
        // Binary frames = audio (OggOpus)
        input.send_audio(msg.to_vec()).await?;
    }
    Ok(())
}

pub async fn serve(config: crate::config::Config) -> Result<()> {
    use std::str::FromStr;

    let llm = Llm::new(
        config.llm_base_url.clone(),
        config.max_completion_tokens.unwrap_or(4096),
        config.llm_model_name.clone(),
        config.llm_api_key.clone(),
    )
    .await?;

    let state = Arc::new(AppState {
        llm: Arc::new(llm),
        gradium_base_url: config.gradium_base_url.clone(),
        pinned: config.pinned.clone(),
        log_sessions: config.log_sessions,
        log_dir: config.log_dir.clone(),
        cnt: std::sync::atomic::AtomicU64::new(0),
    });

    let app = axum::Router::new().route("/ws", axum::routing::get(ws_handler));

    let trace_layer =
        tower::ServiceBuilder::new().layer(tower_http::trace::TraceLayer::new_for_http());

    let app = app.layer(trace_layer).with_state(state);

    let sock_addr = std::net::SocketAddr::from((
        std::net::IpAddr::from_str(&config.addr)
            .unwrap_or(std::net::IpAddr::V6(std::net::Ipv6Addr::LOCALHOST)),
        config.port,
    ));

    tracing::info!("starting gradbot_server on {sock_addr}");
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
