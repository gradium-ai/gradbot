//! Remote mode: connects to a gradbot_server over WebSocket.
//!
//! Provides `RemoteInputHandle` and `RemoteOutputHandle` that proxy
//! audio/config/tool-results over a WS connection, making remote sessions
//! transparent to the Python caller.

use anyhow::{Context, Result};
use futures_util::{SinkExt, StreamExt};
use pyo3::prelude::*;
use serde::{Deserialize, Serialize};
use tokio::sync::mpsc;

use crate::{Event, MsgOut, ToolCallHandleInner, ToolCallHandlePy, ToolCallInfo};

// ---------------------------------------------------------------------------
// Wire protocol types (mirrors gradbot_server/src/protocol.rs)
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
// Messages between the Python-facing handles and the WS background tasks
// ---------------------------------------------------------------------------

/// Messages sent from the input handle to the WS writer task.
pub enum WsOutMsg {
    Audio(Vec<u8>),
    Config(SessionConfigWire),
    ToolResult {
        call_id: String,
        result: serde_json::Value,
        is_error: bool,
    },
}

/// Messages received from the WS reader task, delivered to the output handle.
pub enum RemoteMsgOut {
    Audio {
        data: Vec<u8>,
        start_s: f64,
        stop_s: f64,
        turn_idx: u64,
        interrupted: bool,
    },
    TtsText {
        text: String,
        start_s: f64,
        stop_s: f64,
        turn_idx: u64,
    },
    SttText {
        text: String,
        start_s: f64,
    },
    ToolCall {
        call_id: String,
        tool_name: String,
        args: serde_json::Value,
    },
    Event {
        time_s: f64,
        event: String,
    },
    Error {
        message: String,
    },
}

// ---------------------------------------------------------------------------
// Remote handles
// ---------------------------------------------------------------------------

pub struct RemoteInputHandle {
    ws_tx: mpsc::Sender<WsOutMsg>,
}

impl RemoteInputHandle {
    pub async fn send_audio(&self, data: Vec<u8>) -> Result<()> {
        self.ws_tx
            .send(WsOutMsg::Audio(data))
            .await
            .map_err(|_| anyhow::anyhow!("remote session closed"))?;
        Ok(())
    }

    pub async fn send_config(&self, config: SessionConfigWire) -> Result<()> {
        self.ws_tx
            .send(WsOutMsg::Config(config))
            .await
            .map_err(|_| anyhow::anyhow!("remote session closed"))?;
        Ok(())
    }
}

pub struct RemoteOutputHandle {
    msg_rx: mpsc::Receiver<RemoteMsgOut>,
    ws_tx: mpsc::Sender<WsOutMsg>,
    _reader_task: tokio::task::JoinHandle<()>,
    _writer_task: tokio::task::JoinHandle<()>,
}

impl RemoteOutputHandle {
    pub async fn receive(&mut self) -> Option<RemoteMsgOut> {
        self.msg_rx.recv().await
    }

    pub fn ws_tx(&self) -> mpsc::Sender<WsOutMsg> {
        self.ws_tx.clone()
    }
}

// ---------------------------------------------------------------------------
// Convert RemoteMsgOut to Python MsgOut
// ---------------------------------------------------------------------------

pub fn msgout_from_remote(
    py: Python<'_>,
    msg: RemoteMsgOut,
    ws_tx: mpsc::Sender<WsOutMsg>,
) -> PyResult<MsgOut> {
    match msg {
        RemoteMsgOut::Audio {
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
        RemoteMsgOut::TtsText {
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
        RemoteMsgOut::SttText { text, start_s } => Ok(MsgOut {
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
        RemoteMsgOut::ToolCall {
            call_id,
            tool_name,
            args,
        } => {
            let tool_call_info = ToolCallInfo {
                call_id: call_id.clone(),
                tool_name: tool_name.clone(),
                args_json: args.to_string(),
            };
            let tool_call_py = Py::new(py, tool_call_info)?;
            let handle_py = Py::new(
                py,
                ToolCallHandlePy {
                    inner: Some(ToolCallHandleInner::Remote { call_id, ws_tx }),
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
        RemoteMsgOut::Event { time_s, event } => {
            // Parse event string — it's a JSON-serialized gradbot::Event
            let event_obj = Event {
                event_type: event,
                data: None,
            };
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
        RemoteMsgOut::Error { message } => Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
            "Remote server error: {}",
            message
        ))),
    }
}

// ---------------------------------------------------------------------------
// Connection setup
// ---------------------------------------------------------------------------

/// Connect to a remote gradbot_server and return input/output handles.
pub async fn connect(
    url: String,
    api_key: Option<String>,
    initial_config: Option<SessionConfigWire>,
) -> Result<(RemoteInputHandle, RemoteOutputHandle)> {
    use tokio_tungstenite::tungstenite;

    // Build WS request with auth header
    let mut request = tungstenite::http::Request::builder()
        .uri(&url)
        .header("Host", host_from_url(&url).unwrap_or_default())
        .header("Connection", "Upgrade")
        .header("Upgrade", "websocket")
        .header("Sec-WebSocket-Version", "13")
        .header(
            "Sec-WebSocket-Key",
            tungstenite::handshake::client::generate_key(),
        );

    if let Some(key) = &api_key {
        request = request.header("Authorization", format!("Bearer {}", key));
    }

    let request = request.body(()).context("failed to build WS request")?;

    tracing::info!(url = %url, "connecting to remote gradbot_server");
    let (ws_stream, _response) = tokio_tungstenite::connect_async(request)
        .await
        .context("failed to connect to gradbot_server")?;
    tracing::info!("connected to remote gradbot_server");

    let (ws_sink, ws_stream) = ws_stream.split();

    // Channels between Python handles and WS tasks
    let (ws_out_tx, ws_out_rx) = mpsc::channel::<WsOutMsg>(200);
    let (msg_out_tx, msg_out_rx) = mpsc::channel::<RemoteMsgOut>(100);

    // Send initial config if provided
    if let Some(config) = initial_config {
        let msg = ClientMessage::SessionConfig { config };
        let json = serde_json::to_string(&msg)?;
        // Send through the channel so the writer task handles it
        ws_out_tx
            .send(WsOutMsg::Config(
                // Re-extract config from the message we just built
                serde_json::from_str::<serde_json::Value>(&json)
                    .ok()
                    .and_then(|v| serde_json::from_value(v["config"].clone()).ok())
                    .unwrap_or_default(),
            ))
            .await
            .ok();
    }

    // Spawn WS writer task
    let writer_task = tokio::spawn(ws_writer_loop(ws_sink, ws_out_rx));

    // Spawn WS reader task
    let reader_task = tokio::spawn(ws_reader_loop(ws_stream, msg_out_tx));

    let input = RemoteInputHandle {
        ws_tx: ws_out_tx.clone(),
    };
    let output = RemoteOutputHandle {
        msg_rx: msg_out_rx,
        ws_tx: ws_out_tx,
        _reader_task: reader_task,
        _writer_task: writer_task,
    };

    Ok((input, output))
}

fn host_from_url(url: &str) -> Option<String> {
    url.strip_prefix("wss://")
        .or_else(|| url.strip_prefix("ws://"))
        .and_then(|rest| rest.split('/').next())
        .map(|s| s.to_string())
}

type WsSink = futures_util::stream::SplitSink<
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>,
    tokio_tungstenite::tungstenite::Message,
>;
type WsStream = futures_util::stream::SplitStream<
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>,
>;

/// Background task: reads from input channel, sends to WS.
async fn ws_writer_loop(mut ws_sink: WsSink, mut rx: mpsc::Receiver<WsOutMsg>) {
    use tokio_tungstenite::tungstenite::Message;

    while let Some(msg) = rx.recv().await {
        let result = match msg {
            WsOutMsg::Audio(data) => ws_sink.send(Message::Binary(data)).await,
            WsOutMsg::Config(config) => {
                let client_msg = ClientMessage::SessionConfig { config };
                match serde_json::to_string(&client_msg) {
                    Ok(json) => ws_sink.send(Message::Text(json)).await,
                    Err(e) => {
                        tracing::error!(?e, "failed to serialize config message");
                        continue;
                    }
                }
            }
            WsOutMsg::ToolResult {
                call_id,
                result,
                is_error,
            } => {
                let client_msg = ClientMessage::ToolCallResult {
                    call_id,
                    result,
                    is_error,
                };
                match serde_json::to_string(&client_msg) {
                    Ok(json) => ws_sink.send(Message::Text(json)).await,
                    Err(e) => {
                        tracing::error!(?e, "failed to serialize tool result message");
                        continue;
                    }
                }
            }
        };
        if let Err(e) = result {
            tracing::error!(?e, "WS write error");
            break;
        }
    }
    tracing::info!("WS writer loop ended");
}

/// Background task: reads from WS, writes to output channel.
async fn ws_reader_loop(mut ws_stream: WsStream, tx: mpsc::Sender<RemoteMsgOut>) {
    use tokio_tungstenite::tungstenite::Message;

    // State: when we receive an Audio JSON message, the next binary frame is its data.
    let mut pending_audio: Option<(f64, f64, u64, bool)> = None;

    while let Some(msg) = ws_stream.next().await {
        let msg = match msg {
            Ok(m) => m,
            Err(e) => {
                tracing::error!(?e, "WS read error");
                break;
            }
        };

        match msg {
            Message::Binary(data) => {
                if let Some((start_s, stop_s, turn_idx, interrupted)) = pending_audio.take() {
                    if tx
                        .send(RemoteMsgOut::Audio {
                            data: data.to_vec(),
                            start_s,
                            stop_s,
                            turn_idx,
                            interrupted,
                        })
                        .await
                        .is_err()
                    {
                        break;
                    }
                } else {
                    tracing::warn!("received unexpected binary frame without preceding audio JSON");
                }
            }
            Message::Text(text) => {
                let server_msg: ServerMessage = match serde_json::from_str(&text) {
                    Ok(m) => m,
                    Err(e) => {
                        tracing::warn!(?e, "failed to parse server message");
                        continue;
                    }
                };

                let remote_msg = match server_msg {
                    ServerMessage::Audio {
                        start_s,
                        stop_s,
                        turn_idx,
                        interrupted,
                    } => {
                        // Next binary frame will carry the audio data
                        pending_audio = Some((start_s, stop_s, turn_idx, interrupted));
                        continue;
                    }
                    ServerMessage::TtsText {
                        text,
                        start_s,
                        stop_s,
                        turn_idx,
                    } => RemoteMsgOut::TtsText {
                        text,
                        start_s,
                        stop_s,
                        turn_idx,
                    },
                    ServerMessage::SttText { text, start_s } => {
                        RemoteMsgOut::SttText { text, start_s }
                    }
                    ServerMessage::ToolCall {
                        call_id,
                        tool_name,
                        args,
                    } => RemoteMsgOut::ToolCall {
                        call_id,
                        tool_name,
                        args,
                    },
                    ServerMessage::Event { time_s, event } => RemoteMsgOut::Event { time_s, event },
                    ServerMessage::Error { message } => RemoteMsgOut::Error { message },
                    ServerMessage::SessionConfigApplied { pinned_fields } => {
                        if !pinned_fields.is_empty() {
                            tracing::info!(?pinned_fields, "server pinned config fields");
                        }
                        continue;
                    }
                };

                if tx.send(remote_msg).await.is_err() {
                    break;
                }
            }
            Message::Close(_) => {
                tracing::info!("WS closed by server");
                break;
            }
            Message::Ping(_) | Message::Pong(_) => {}
            _ => {}
        }
    }
    tracing::info!("WS reader loop ended");
}
