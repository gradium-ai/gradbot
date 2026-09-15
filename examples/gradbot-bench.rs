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
//!
//! `--repetitions` replays the manifest that many times, opening a **fresh
//! session per repetition** (never more than one at a time — see
//! `run_session`): prod backends carry shared load and network jitter, so a
//! single measurement carries essentially no information, and reusing one
//! session would let state from earlier turns bleed into later repetitions.
//! `e2e_ms` from every repetition is pooled by fixture `category` and reduced
//! with `summarize`, which refuses to report a summary for fewer than two
//! samples.
//!
//! A WebSocket RTT probe (`probe_rtt_ms`) runs once per invocation, before any
//! fixture traffic, on its own short-lived connection so it never competes
//! with the traffic being measured. It exists to sanity-check `network_ms`
//! (Task 13), a residual computed as end-to-end minus everything the server
//! can account for — trustworthy only if it lands near an independently
//! measured transport cost (`residual_is_plausible`).

use anyhow::{Context, Result};
use clap::Parser;
use futures_util::StreamExt;
use gradbot_bin::openai_protocol as oai;
use std::collections::BTreeMap;
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

/// Nearest-rank percentile. `sorted` must be ascending.
pub fn percentile(sorted: &[f64], p: f64) -> f64 {
    assert!(!sorted.is_empty(), "percentile of an empty slice");
    let rank = (p * (sorted.len() - 1) as f64).round() as usize;
    sorted[rank.min(sorted.len() - 1)]
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct Summary {
    pub n: usize,
    pub p50: f64,
    pub p90: f64,
    pub p99: f64,
}

/// Summarizes repeated measurements. Returns `None` for fewer than two
/// samples: against prod backends a single run carries no information, and
/// reporting one as a result would be misleading.
pub fn summarize(values: &[f64]) -> Option<Summary> {
    if values.len() < 2 {
        return None;
    }
    let mut sorted = values.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).expect("NaN in latency samples"));
    Some(Summary {
        n: sorted.len(),
        p50: percentile(&sorted, 0.5),
        p90: percentile(&sorted, 0.9),
        p99: percentile(&sorted, 0.99),
    })
}

/// Whether a computed `network_ms` residual is physically believable given an
/// independently measured round trip. A negative residual means the join is
/// wrong; a residual far above the RTT means server time went unattributed.
/// Either way the breakdown is not trustworthy and must be fixed before any
/// optimization decision rests on it.
pub fn residual_is_plausible(network_ms: f64, rtt_p50_ms: f64) -> bool {
    network_ms >= 0.0 && network_ms <= rtt_p50_ms * 4.0 + 50.0
}

/// Measures WebSocket round-trip time with `n` ping/pong exchanges. Run once
/// per invocation, before fixture playback begins, so it never competes with
/// the traffic being measured.
pub async fn probe_rtt_ms(ws: &mut WebSocket, n: usize) -> Result<Summary> {
    use futures_util::SinkExt;
    let mut samples = Vec::with_capacity(n);
    for i in 0..n {
        let started = Instant::now();
        ws.send(ws::Message::Ping(vec![i as u8])).await?;
        // Drain until the matching pong; other frames may interleave.
        while let Some(msg) = ws.next().await {
            if let ws::Message::Pong(_) = msg? {
                samples.push(started.elapsed().as_secs_f64() * 1000.0);
                break;
            }
        }
    }
    summarize(&samples).ok_or_else(|| anyhow::anyhow!("rtt probe collected < 2 samples"))
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

/// Client marks recorded during a single fixture manifest replay, plus the
/// once-per-run RTT probe, for `--out-marks`.
#[derive(Debug, Clone, serde::Serialize)]
struct BenchOutput {
    rtt_probe_ms: Summary,
    marks: Vec<ClientMark>,
}

/// Streams one full pass of `manifest` over a **fresh** connection and
/// returns the `ClientMark`s recorded during it. The connection is opened at
/// the start of this call and dropped when it returns, so callers that loop
/// over repetitions naturally get one session at a time, never concurrent
/// ones — reusing a session across repetitions would let state from earlier
/// turns bleed into later ones, and running two sessions at once would
/// contend for the same backends and confound the very numbers being
/// measured.
async fn run_session(url: &str, manifest: &Manifest) -> Result<Vec<ClientMark>> {
    let connection = Connection::new(url).await?;
    let (mut sender, mut receiver) = connection.split();

    // One origin per session; every mark is stamped from it.
    let origin = Instant::now();
    let marks: Arc<Mutex<Vec<ClientMark>>> = Arc::new(Mutex::new(Vec::new()));
    // Turn the receive loop should attribute incoming audio to. See module
    // docs: the wire protocol has no turn id, so this is how the two loops
    // agree on one.
    let current_turn = Arc::new(AtomicU64::new(0));

    let send_loop = {
        let marks = marks.clone();
        let current_turn = current_turn.clone();
        async move {
            use futures_util::SinkExt;
            use oai::ClientEvent as CE;

            let mut opus_encoder = kaudio::ogg_opus::Encoder::new(IN_SAMPLE_RATE as usize)?;
            let header = opus_encoder.header_data().to_vec();
            let msg = serde_json::to_string(&CE::input_audio_buffer_append(header))?;
            sender.ws_sender.send(ws::Message::Text(msg)).await?;

            // Cumulative samples written to the socket across the whole
            // session (speech and silence, every turn) — this is what both
            // the pacing clock and `ClientMark.sample_idx` are measured
            // against, matching the server's own running sample counter.
            let mut total_samples: u64 = 0;
            let mut turn: u64 = 0;

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

    let marks = marks.lock().unwrap().clone();
    Ok(marks)
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    let manifest = Manifest::load(&args.manifest)?;

    // Measure transport RTT once, before any fixture traffic, on its own
    // short-lived connection — dropped before the first repetition's session
    // opens — so the probe can never compete with the traffic being measured.
    let mut probe_conn = Connection::new(&args.url).await?;
    let rtt_probe_ms = probe_rtt_ms(&mut probe_conn.ws, 20).await?;
    println!(
        "RTT probe (n={}): p50={:.2}ms p90={:.2}ms p99={:.2}ms",
        rtt_probe_ms.n, rtt_probe_ms.p50, rtt_probe_ms.p90, rtt_probe_ms.p99
    );
    drop(probe_conn);

    let mut all_marks: Vec<ClientMark> = Vec::new();
    // e2e_ms pooled by fixture category across every repetition.
    let mut by_category: BTreeMap<String, Vec<f64>> = BTreeMap::new();

    for rep in 0..args.repetitions {
        println!(
            "--- repetition {}/{} ({}) ---",
            rep + 1,
            args.repetitions,
            args.url
        );
        let marks = run_session(&args.url, &manifest).await?;
        for (idx, fturn) in manifest.turns.iter().enumerate() {
            if let Some(ms) = e2e_ms(&marks, idx as u64) {
                by_category.entry(fturn.category.clone()).or_default().push(ms);
            }
        }
        all_marks.extend(marks);
    }

    for (category, values) in &by_category {
        match summarize(values) {
            Some(s) => println!(
                "{category}: n={} p50={:.2}ms p90={:.2}ms p99={:.2}ms",
                s.n, s.p50, s.p90, s.p99
            ),
            None => println!(
                "{category}: only {} sample(s), refusing to summarize",
                values.len()
            ),
        }
    }

    let out = std::fs::File::create(&args.out_marks)?;
    serde_json::to_writer_pretty(
        out,
        &BenchOutput {
            rtt_probe_ms,
            marks: all_marks,
        },
    )?;

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

    #[test]
    fn percentiles_use_nearest_rank() {
        let sorted = vec![10.0, 20.0, 30.0, 40.0, 50.0];
        assert_eq!(percentile(&sorted, 0.5), 30.0);
        assert_eq!(percentile(&sorted, 0.9), 50.0);
        assert_eq!(percentile(&sorted, 0.0), 10.0);
        assert_eq!(percentile(&sorted, 1.0), 50.0);
    }

    #[test]
    fn summarize_sorts_its_input() {
        let s = summarize(&[50.0, 10.0, 30.0, 20.0, 40.0]).unwrap();
        assert_eq!(s.n, 5);
        assert_eq!(s.p50, 30.0);
    }

    #[test]
    fn summarize_refuses_a_single_sample() {
        // Prod backend jitter makes one sample meaningless.
        assert!(summarize(&[42.0]).is_none());
        assert!(summarize(&[]).is_none());
    }

    /// The report's `network_ms` is a residual — everything E2E could not
    /// attribute to the server. A residual is only trustworthy if it lands near an
    /// independently measured transport cost, so the harness measures one.
    #[test]
    fn network_residual_is_plausible_against_a_measured_rtt() {
        // One-way transport is roughly half the round trip; allow generous slack
        // for asymmetry and the client's own decode cost.
        assert!(residual_is_plausible(12.0, 20.0), "12ms residual vs 20ms RTT is fine");
        assert!(!residual_is_plausible(-5.0, 20.0), "a negative residual means the join is wrong");
        assert!(
            !residual_is_plausible(400.0, 20.0),
            "a residual 20x the RTT means unattributed server time, not network"
        );
    }
}
