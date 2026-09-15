//! Real-time fixture player and client-side latency marks.
//!
//! Streams pre-recorded utterances from a fixture manifest into a live
//! gradbot session at real speed and records two client-side timestamps per
//! turn: when the user's speech ends, and when the agent's first real audio
//! arrives. The gap between them is the number that matters to a user: how
//! long after they stop speaking does the agent start talking.
//!
//! Pacing (the `sleep_until` against a fixed clock origin, computed from a
//! cumulative sample count rather than per-chunk sleeps) is copied verbatim
//! from `examples/gradbot-client.rs` — cumulative sleeps drift, and drift
//! would silently inflate every latency number this harness measures.
//!
//! The wire protocol carries no turn id, so incoming audio is attributed to
//! a turn via `current_turn`, a value shared between the send and receive
//! loops: the send loop advances it the moment it marks `UserSpeechEnd` for a
//! turn (audio arriving before that instant can only be a stale reply to the
//! previous turn), and the receive loop tags the first real-audio packet it
//! sees after that with the same turn number.

use anyhow::{Context, Result};
use clap::Parser;
use futures_util::StreamExt;
use gradbot_bin::openai_protocol as oai;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio_tungstenite::tungstenite as ws;

#[path = "bench/fixtures.rs"]
mod fixtures;

use fixtures::Manifest;

pub type WebSocket =
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;
pub type WebSocketSender = futures_util::stream::SplitSink<WebSocket, ws::Message>;
pub type WebSocketReceiver = futures_util::stream::SplitStream<WebSocket>;

const IN_SAMPLE_RATE: u32 = 24000;
const IN_CHUNK_SIZE: usize = 1920;
const OUT_SAMPLE_RATE: usize = 48000;

struct Connection {
    ws: WebSocket,
}

struct Sender {
    ws_sender: WebSocketSender,
}

struct Receiver {
    ws_receiver: WebSocketReceiver,
}

impl Receiver {
    async fn next(&mut self) -> Option<Result<ws::Message>> {
        let msg = self.ws_receiver.next().await?;
        let msg = match msg {
            Ok(m) => Ok(m),
            Err(e) => Err(e.into()),
        };
        Some(msg)
    }
}

impl Connection {
    pub async fn new(url: &str) -> Result<Self> {
        let (ws, _response) = tokio_tungstenite::connect_async_with_config(url, None, true).await?;
        Ok(Self { ws })
    }

    fn split(self) -> (Sender, Receiver) {
        let (ws_sender, ws_receiver) = self.ws.split();
        (Sender { ws_sender }, Receiver { ws_receiver })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MarkKind {
    /// The last sample of this turn's speech was written to the socket.
    UserSpeechEnd,
    /// The first agent audio packet carrying real PCM arrived.
    FirstAgentAudio,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct ClientMark {
    /// `u64` to match `TraceRecord.turn`, which Task 13 compares it against.
    pub turn: u64,
    pub kind: MarkKind,
    /// Microseconds since the harness clock origin (its own monotonic Instant).
    pub t_us: u64,
    /// Cumulative input sample index — the join key against the server trace.
    pub sample_idx: u64,
}

/// True when a decoded packet carries audio. Opus header packets decode to
/// nothing and must never be treated as the agent's first audio.
pub fn is_agent_audio(decoded: &[f32]) -> bool {
    !decoded.is_empty()
}

/// End-to-end latency for one turn: first agent audio minus user speech end,
/// both on the harness's own monotonic clock. Returns `None` if the agent
/// never answered.
pub fn e2e_ms(marks: &[ClientMark], turn: u64) -> Option<f64> {
    let end = marks
        .iter()
        .find(|m| m.turn == turn && m.kind == MarkKind::UserSpeechEnd)?;
    let audio = marks
        .iter()
        .find(|m| m.turn == turn && m.kind == MarkKind::FirstAgentAudio)?;
    Some((audio.t_us as f64 - end.t_us as f64) / 1000.0)
}

#[derive(Parser, Debug)]
struct Args {
    /// Server WebSocket URL, e.g. ws://localhost:8000/v1/realtime.
    #[clap(long)]
    url: String,

    /// Fixture manifest JSON path.
    #[clap(long)]
    manifest: PathBuf,

    /// Number of times to replay the manifest.
    #[clap(long, default_value_t = 1)]
    repetitions: usize,

    /// Where to write the recorded `ClientMark` JSON array.
    #[clap(long)]
    out_marks: PathBuf,
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    let manifest = Manifest::load(&args.manifest)?;

    let connection = Connection::new(&args.url).await?;
    println!("Connected to {}", args.url);
    let (mut sender, mut receiver) = connection.split();

    // One origin for the whole run; every mark is stamped from it.
    let origin = Instant::now();
    let marks: Arc<Mutex<Vec<ClientMark>>> = Arc::new(Mutex::new(Vec::new()));
    // Turn the receive loop should attribute incoming audio to. See module
    // docs: the wire protocol has no turn id, so this is how the two loops
    // agree on one.
    let current_turn = Arc::new(AtomicU64::new(0));

    let send_loop = {
        let marks = marks.clone();
        let current_turn = current_turn.clone();
        let repetitions = args.repetitions;
        async move {
            use futures_util::SinkExt;
            use oai::ClientEvent as CE;

            let mut opus_encoder = kaudio::ogg_opus::Encoder::new(IN_SAMPLE_RATE as usize)?;
            let header = opus_encoder.header_data().to_vec();
            let msg = serde_json::to_string(&CE::input_audio_buffer_append(header))?;
            sender.ws_sender.send(ws::Message::Text(msg)).await?;

            // Cumulative samples written to the socket across the whole run
            // (speech and silence, every turn and repetition) — this is what
            // both the pacing clock and `ClientMark.sample_idx` are measured
            // against, matching the server's own running sample counter.
            let mut total_samples: u64 = 0;
            let mut turn: u64 = 0;

            for _rep in 0..repetitions {
                for fturn in &manifest.turns {
                    let (pcm, sr) = kaudio::pcm_decode(&fturn.wav).with_context(|| {
                        format!("decoding fixture wav {}", fturn.wav.display())
                    })?;
                    let pcm = if sr != IN_SAMPLE_RATE {
                        kaudio::resample(&pcm, sr as usize, IN_SAMPLE_RATE as usize)?
                    } else {
                        pcm
                    };

                    let mut wav_pos: u64 = 0;
                    let mut marked_end = false;
                    for chunk in pcm.chunks(IN_CHUNK_SIZE) {
                        let millis = total_samples * 1000 / IN_SAMPLE_RATE as u64;
                        let target_time = origin + Duration::from_millis(millis);
                        tokio::time::sleep_until(target_time.into()).await;

                        let encoded = opus_encoder.encode_page(chunk)?;
                        let out_msg =
                            serde_json::to_string(&CE::input_audio_buffer_append(encoded))?;
                        sender.ws_sender.send(ws::Message::Text(out_msg)).await?;

                        wav_pos += chunk.len() as u64;
                        total_samples += chunk.len() as u64;

                        if !marked_end && wav_pos >= fturn.speech_end_sample {
                            marked_end = true;
                            current_turn.store(turn, Ordering::SeqCst);
                            marks.lock().unwrap().push(ClientMark {
                                turn,
                                kind: MarkKind::UserSpeechEnd,
                                t_us: origin.elapsed().as_micros() as u64,
                                sample_idx: total_samples,
                            });
                        }
                    }

                    // Keep streaming silence after the turn: the server's VAD
                    // needs continuing frames to detect end-of-turn, and the
                    // stream must never stop between turns.
                    let gap_samples = (fturn.gap_after_s * IN_SAMPLE_RATE as f64) as u64;
                    let mut gap_streamed: u64 = 0;
                    while gap_streamed < gap_samples {
                        let chunk_len =
                            (gap_samples - gap_streamed).min(IN_CHUNK_SIZE as u64) as usize;
                        let millis = total_samples * 1000 / IN_SAMPLE_RATE as u64;
                        let target_time = origin + Duration::from_millis(millis);
                        tokio::time::sleep_until(target_time.into()).await;

                        let silence = vec![0f32; chunk_len];
                        let encoded = opus_encoder.encode_page(&silence)?;
                        let out_msg =
                            serde_json::to_string(&CE::input_audio_buffer_append(encoded))?;
                        sender.ws_sender.send(ws::Message::Text(out_msg)).await?;

                        gap_streamed += chunk_len as u64;
                        total_samples += chunk_len as u64;
                    }

                    turn += 1;
                }
            }
            Ok::<_, anyhow::Error>(())
        }
    };

    let recv_loop = {
        let marks = marks.clone();
        let current_turn = current_turn.clone();
        async move {
            // No buffering: flush whatever decoded on every packet so the
            // first real-audio packet is caught as early as possible. This
            // also means a header-only packet decodes to `Some(&[])` rather
            // than `None` — exactly the case `is_agent_audio` exists to
            // reject, instead of silently absorbing it into the next batch.
            let mut opus_decoder = kaudio::ogg_opus::Decoder::new(OUT_SAMPLE_RATE, 0)?;
            let mut last_marked_turn: Option<u64> = None;
            while let Some(msg) = receiver.next().await {
                let msg: oai::ServerEvent = match msg? {
                    ws::Message::Text(b) => serde_json::from_str(&b)?,
                    ws::Message::Binary(b) => serde_json::from_slice(&b)?,
                    ws::Message::Close(_) => break,
                    ws::Message::Frame(_) | ws::Message::Ping(_) | ws::Message::Pong(_) => continue,
                };
                match msg {
                    oai::ServerEvent::Error { event_id: _, error } => {
                        anyhow::bail!("Error from server: {error:?}");
                    }
                    oai::ServerEvent::ResponseAudioDelta { event_id: _, delta } => {
                        if let Some(decoded) = opus_decoder.decode(&delta)? {
                            if is_agent_audio(decoded) {
                                let turn = current_turn.load(Ordering::SeqCst);
                                if last_marked_turn != Some(turn) {
                                    last_marked_turn = Some(turn);
                                    marks.lock().unwrap().push(ClientMark {
                                        turn,
                                        kind: MarkKind::FirstAgentAudio,
                                        t_us: origin.elapsed().as_micros() as u64,
                                        sample_idx: 0,
                                    });
                                }
                            }
                        }
                    }
                    _ => {}
                }
            }
            Ok::<_, anyhow::Error>(())
        }
    };

    tokio::select! {
        res = send_loop => pp_err("send_loop", res),
        res = recv_loop => pp_err("recv_loop", res),
    }

    let marks = marks.lock().unwrap();
    let out = std::fs::File::create(&args.out_marks)?;
    serde_json::to_writer_pretty(out, &*marks)?;

    Ok(())
}

fn pp_err(name: &str, res: Result<()>) {
    match res {
        Ok(()) => tracing::info!("{name} ended normally"),
        Err(err) => tracing::error!("{name} error: {err}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn e2e_is_first_agent_audio_minus_user_speech_end() {
        let marks = vec![
            ClientMark { turn: 0, kind: MarkKind::UserSpeechEnd, t_us: 1_000_000, sample_idx: 24000 },
            ClientMark { turn: 0, kind: MarkKind::FirstAgentAudio, t_us: 1_850_000, sample_idx: 0 },
        ];
        assert_eq!(e2e_ms(&marks, 0), Some(850.0));
    }

    #[test]
    fn e2e_is_none_when_the_agent_never_answered() {
        let marks = vec![ClientMark {
            turn: 0,
            kind: MarkKind::UserSpeechEnd,
            t_us: 1_000_000,
            sample_idx: 24000,
        }];
        assert_eq!(e2e_ms(&marks, 0), None);
    }

    /// The single easiest way to produce a fake good number: Opus header
    /// packets carry no audio and are emitted on every turn change. Counting
    /// one as first audio reports a near-zero latency that is pure fiction.
    #[test]
    fn header_packets_are_not_first_audio() {
        assert!(!is_agent_audio(&[]), "an empty decode is not audio");
        assert!(is_agent_audio(&[0.0, 0.1]), "a non-empty decode is audio");
    }
}
