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
//! The receive loop outlives the send loop: once the last fixture turn (and
//! its trailing silence) has been written, the session keeps receiving for
//! `DRAIN_AFTER_LAST_TURN` before tearing down, so a late answer to the final
//! turn is still observed rather than counted as a missed endpoint. A session
//! loop that ends in an *error* is recorded as a `RunFailure` and printed to
//! stderr — a dead session must never be reported as the agent declining to
//! answer.
//!
//! `--repetitions` replays the manifest that many times, opening a **fresh
//! session per repetition** (never more than one at a time — see
//! `run_session`): prod backends carry shared load and network jitter, so a
//! single measurement carries essentially no information, and reusing one
//! session would let state from earlier turns bleed into later repetitions.
//! Because each repetition is a fresh session, both the client's `turn`
//! counter and the server's per-session sample counter restart at zero every
//! time — so every `ClientMark` also carries `repetition`, and `e2e_ms`
//! matches on `(repetition, turn)`, never `turn` alone, to avoid silently
//! conflating marks from different sessions that happen to share a turn and
//! sample index. `e2e_ms` from every repetition is pooled by fixture
//! `category` and reduced with `summarize`, which refuses to report a
//! summary for fewer than two samples.
//!
//! A WebSocket RTT probe (`probe_rtt_ms`) runs once per invocation, on
//! **repetition 0's own connection**, after the handshake but before that
//! repetition's playback begins — so it still never competes with the traffic
//! being measured, and still costs nothing in later repetitions. It must not
//! run on a throwaway connection of its own: the server creates a tracer, and
//! therefore a `trace_*.jsonl` file, per accepted connection, so a probe
//! connection leaves an empty trace file that sorts *first* by timestamp.
//! `breakdown_all` would then pair repetition *i* with repetition *i-1*'s
//! trace — which is exactly why it refuses to run at all when the trace-file
//! count does not match the repetition count. The probe exists to
//! sanity-check `network_ms` (Task 13), a residual computed as end-to-end
//! minus everything the server can account for — trustworthy only if it lands
//! near an independently measured transport cost (`residual_is_plausible`).

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
#[path = "bench/report.rs"]
mod report;

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
    /// Zero-based repetition index. Each repetition opens a **fresh
    /// session** (see `run_session`), so both `turn` (the client's own
    /// per-session counter) and `sample_idx` (the server's per-session
    /// `samples_sent` counter) restart at zero every repetition and are
    /// therefore only unique *within* one. `repetition` is what scopes
    /// them — never join or look up a mark by `turn` or `sample_idx` alone
    /// across a multi-repetition run.
    pub repetition: u64,
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

/// End-to-end latency for one turn of one repetition: first agent audio
/// minus user speech end, both on the harness's own monotonic clock. Returns
/// `None` if the agent never answered. Matches on `(repetition, turn)`, not
/// `turn` alone — `turn` resets to zero every repetition, so matching on it
/// alone would silently conflate marks from different sessions.
pub fn e2e_ms(marks: &[ClientMark], repetition: u64, turn: u64) -> Option<f64> {
    let end = marks.iter().find(|m| {
        m.repetition == repetition && m.turn == turn && m.kind == MarkKind::UserSpeechEnd
    })?;
    let audio = marks.iter().find(|m| {
        m.repetition == repetition && m.turn == turn && m.kind == MarkKind::FirstAgentAudio
    })?;
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

/// Bound on how long a single ping waits for its pong. This probe runs
/// before any fixture traffic, so a peer that accepts the WebSocket
/// handshake but never sends a pong back — a wedged process, a proxy that
/// swallows control frames, a half-open connection — must not be allowed to
/// hang the whole benchmark indefinitely with zero output. A miss is skipped
/// (see `probe_rtt_ms`), not fatal; do not remove this timeout to
/// "simplify" the loop back to the brief's original unbounded sketch.
const RTT_PING_TIMEOUT: Duration = Duration::from_secs(5);

/// How long the receive loop keeps running after the send loop has written
/// the last fixture turn (and its trailing `gap_after_s` of silence), before
/// the session is torn down.
///
/// Five seconds: comfortably longer than any end-to-end latency this harness
/// is meant to measure, short enough that it costs seconds, not minutes, at
/// high `--repetitions`. Without a drain the session died the instant
/// playback ended, so an answer to the *last* turn that arrived after its
/// `gap_after_s` was never observed — on a single-turn manifest that is a
/// 100% `missed_endpoint_rate` for a server that answered correctly.
const DRAIN_AFTER_LAST_TURN: Duration = Duration::from_secs(5);

/// Number of ping/pong exchanges the RTT probe attempts. `summarize` refuses
/// fewer than two samples, so this is sized to survive a handful of misses.
const RTT_PROBE_PINGS: usize = 20;

/// Waits for the pong matching an already-sent ping, draining and discarding
/// any other frames that interleave. Factored out of `probe_rtt_ms` so the
/// timeout in that function wraps a single, ordinary future.
async fn wait_for_pong(ws: &mut WebSocket) -> Result<()> {
    while let Some(msg) = ws.next().await {
        if let ws::Message::Pong(_) = msg? {
            return Ok(());
        }
    }
    anyhow::bail!("connection closed while waiting for a pong")
}

/// Measures WebSocket round-trip time with up to `n` ping/pong exchanges. Run
/// once per invocation, on repetition 0's connection before its fixture
/// playback begins, so it never competes with the traffic being measured and
/// never opens a connection of its own (see the module docs: an extra
/// connection means an extra server-side trace file). Draining non-pong
/// frames while waiting is safe here only because the server sends nothing
/// before it receives audio. A ping that does not get a pong back
/// within `RTT_PING_TIMEOUT` is skipped rather than treated as fatal; only
/// ending up with fewer than two samples overall is an error, since a
/// transport this harness cannot characterize must fail loudly rather than
/// silently produce a bogus baseline.
pub async fn probe_rtt_ms(ws: &mut WebSocket, n: usize) -> Result<Summary> {
    use futures_util::SinkExt;
    let mut samples = Vec::with_capacity(n);
    for i in 0..n {
        let started = Instant::now();
        ws.send(ws::Message::Ping(vec![i as u8])).await?;
        match tokio::time::timeout(RTT_PING_TIMEOUT, wait_for_pong(ws)).await {
            Ok(Ok(())) => samples.push(started.elapsed().as_secs_f64() * 1000.0),
            Ok(Err(_)) | Err(_) => continue, // read error, close, or timeout: skip this sample
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

    /// Directory the server's per-session trace files
    /// (`trace_<unix_nanos>_<counter>.jsonl`) were written to — must match
    /// the server's own `trace_dir` config. Required to render `--out-report`.
    #[clap(long)]
    trace_dir: Option<PathBuf>,

    /// Where to write the rendered markdown latency report. Requires
    /// `--trace-dir`, since the waterfall and stage split are joined against
    /// the server's trace files, not derivable from client marks alone.
    #[clap(long)]
    out_report: Option<PathBuf>,

    /// Whether the LLM under measurement is co-located with this harness
    /// (e.g. a self-hosted vLLM on the same cluster), as opposed to a remote
    /// production LLM. A co-located LLM has ~zero network cost, so the
    /// report labels its stage `measured-local` to stop a reader concluding
    /// the LLM is cheap and optimizing the orchestration against a network
    /// cost that would not exist in production. The harness cannot detect
    /// this on its own (it does not know the server's LLM configuration) —
    /// state it explicitly rather than guessing from the URL. Defaults to
    /// `true` because every backend this harness currently measures against
    /// is a co-located vLLM; pass `--llm-local=false` when pointing it at a
    /// remote LLM.
    #[clap(long, action = clap::ArgAction::Set, default_value_t = true)]
    llm_local: bool,
}

/// Client marks recorded during a single fixture manifest replay, plus the
/// once-per-run RTT probe, for `--out-marks`.
#[derive(Debug, Clone, serde::Serialize)]
struct BenchOutput {
    /// `None` only when no repetition ran at all (`--repetitions 0`): the
    /// probe rides on repetition 0's connection, and a probe that cannot
    /// collect two samples is fatal rather than absent.
    rtt_probe_ms: Option<Summary>,
    /// Repetitions whose session did not complete. Non-empty means the marks
    /// below are incomplete — see [`RunFailure`].
    failures: Vec<RunFailure>,
    marks: Vec<ClientMark>,
}

/// Everything one `run_session` call produced.
struct SessionOutcome {
    marks: Vec<ClientMark>,
    /// `Some` only for the repetition that was asked to probe (see
    /// `run_session`'s `rtt_probe_pings`).
    rtt_probe_ms: Option<Summary>,
    /// `Some` when this repetition's session did not complete cleanly (see
    /// [`RunFailure`]). The marks it produced are still returned — they are
    /// just incomplete, and the report must say so.
    failure: Option<String>,
}

/// A repetition whose session ended before the manifest did.
///
/// This exists because the failure is otherwise invisible *and*
/// indistinguishable from a real result: a server error or a dropped socket
/// truncates the marks, and every turn that never played then has no
/// `FirstAgentAudio` mark, which `missed_endpoint_rate` reports as "the agent
/// never answered". The run must say "this session died" instead of blaming
/// endpointing for it.
#[derive(Debug, Clone, serde::Serialize)]
pub struct RunFailure {
    pub repetition: u64,
    pub error: String,
}

/// Streams one full pass of `manifest` over a **fresh** connection and
/// returns the `ClientMark`s recorded during it, all stamped with
/// `repetition` (see `ClientMark::repetition` for why: this session's own
/// `turn` counter and the server's `samples_sent` counter both restart at
/// zero, so `repetition` is the only thing that disambiguates this
/// session's marks from any other). The connection is opened at the start of
/// this call and dropped when it returns, so callers that loop over
/// repetitions naturally get one session at a time, never concurrent ones —
/// reusing a session across repetitions would let state from earlier turns
/// bleed into later ones, and running two sessions at once would contend for
/// the same backends and confound the very numbers being measured.
///
/// When `rtt_probe_pings` is `Some(n)`, the transport RTT probe runs on *this*
/// connection, after the handshake and before any fixture audio is written.
/// It deliberately does not get a connection of its own: the server creates a
/// tracer — and eagerly creates its `trace_*.jsonl` file — per accepted
/// connection, so a throwaway probe connection would leave an extra, empty
/// trace file that sorts first by timestamp and breaks the strict
/// trace-file-count-equals-repetition-count invariant `breakdown_all` relies
/// on. Running before playback keeps the probe off the measured path.
async fn run_session(
    url: &str,
    manifest: &Manifest,
    repetition: u64,
    rtt_probe_pings: Option<usize>,
) -> Result<SessionOutcome> {
    let mut connection = Connection::new(url).await?;
    let rtt_probe_ms = match rtt_probe_pings {
        Some(n) => Some(probe_rtt_ms(&mut connection.ws, n).await?),
        None => None,
    };
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
                            repetition,
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
                                        repetition,
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

    // The receive loop is spawned rather than `select!`ed as a bare future:
    // `select!` drops the loser, so the old code tore the receiver down the
    // instant the send loop finished and never saw an answer to the last turn
    // (see `DRAIN_AFTER_LAST_TURN`). Spawning keeps it alive for the drain
    // below; it owns only `Arc`s and its own half of the socket, so it is
    // `'static`. The send loop borrows `manifest` and stays inline.
    let mut recv_task = tokio::spawn(recv_loop);
    tokio::pin!(send_loop);

    let (mut failure, recv_ended_first) = tokio::select! {
        res = &mut send_loop => (loop_failure(repetition, "send_loop", res), false),
        res = &mut recv_task => (join_failure(repetition, "recv_loop", res), true),
    };

    if recv_ended_first {
        // The receiver finished while there was still audio to play: the
        // server errored, closed, or the socket dropped. Even when the loop
        // itself returned `Ok` (a clean `Close` frame is `Ok`), the session
        // is truncated and every unplayed turn will look like a missed
        // endpoint. Say so rather than letting the report blame endpointing.
        failure = failure.or_else(|| {
            let msg = "recv_loop ended before playback finished: the server closed \
                       the connection mid-manifest, so the remaining turns never ran"
                .to_string();
            eprintln!("bench: repetition {repetition}: {msg}");
            Some(msg)
        });
    } else {
        match tokio::time::timeout(DRAIN_AFTER_LAST_TURN, &mut recv_task).await {
            // Receiver stopped on its own inside the window.
            Ok(res) => failure = failure.or_else(|| join_failure(repetition, "recv_loop", res)),
            // Still receiving when the window closed — the normal ending.
            Err(_) => recv_task.abort(),
        }
    }

    let marks = marks.lock().unwrap().clone();
    Ok(SessionOutcome {
        marks,
        rtt_probe_ms,
        failure,
    })
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    let manifest = Manifest::load(&args.manifest)?;

    // Transport RTT is measured once, on repetition 0's own connection before
    // its playback starts. Not on a connection of its own: the server writes
    // one trace file per accepted connection, so a throwaway probe connection
    // would leave an N+1st (empty, earliest) trace file and make every
    // `--out-report` run abort on the trace-file/repetition count check.
    let mut rtt_probe_ms: Option<Summary> = None;

    let mut all_marks: Vec<ClientMark> = Vec::new();
    let mut failures: Vec<RunFailure> = Vec::new();
    // e2e_ms pooled by fixture category across every repetition.
    let mut by_category: BTreeMap<String, Vec<f64>> = BTreeMap::new();

    for rep in 0..args.repetitions {
        println!(
            "--- repetition {}/{} ({}) ---",
            rep + 1,
            args.repetitions,
            args.url
        );
        let probe_pings = if rep == 0 {
            Some(RTT_PROBE_PINGS)
        } else {
            None
        };
        let outcome = run_session(&args.url, &manifest, rep as u64, probe_pings).await?;
        let marks = outcome.marks;
        if let Some(error) = outcome.failure {
            failures.push(RunFailure {
                repetition: rep as u64,
                error,
            });
        }
        if let Some(rtt) = outcome.rtt_probe_ms {
            println!(
                "RTT probe (n={}): p50={:.2}ms p90={:.2}ms p99={:.2}ms",
                rtt.n, rtt.p50, rtt.p90, rtt.p99
            );
            rtt_probe_ms = Some(rtt);
        }
        for (idx, fturn) in manifest.turns.iter().enumerate() {
            if let Some(ms) = e2e_ms(&marks, rep as u64, idx as u64) {
                by_category.entry(fturn.category.clone()).or_default().push(ms);
            }
        }
        all_marks.extend(marks);
    }

    let mut category_summaries: BTreeMap<String, Summary> = BTreeMap::new();
    for (category, values) in &by_category {
        match summarize(values) {
            Some(s) => {
                println!(
                    "{category}: n={} p50={:.2}ms p90={:.2}ms p99={:.2}ms",
                    s.n, s.p50, s.p90, s.p99
                );
                category_summaries.insert(category.clone(), s);
            }
            None => println!(
                "{category}: only {} sample(s), refusing to summarize",
                values.len()
            ),
        }
    }

    if let Some(out_report) = &args.out_report {
        let trace_dir = args
            .trace_dir
            .as_ref()
            .context("--out-report requires --trace-dir (where the server wrote its trace files)")?;
        let breakdowns = report::breakdown_all(&all_marks, trace_dir)?;
        // A turn that could not be decomposed is recorded, not fatal (see
        // `report::TurnFailure`) — but it must be visible without opening the
        // report, since the endpointing sweep runs unattended.
        if !breakdowns.unmeasurable.is_empty() {
            eprintln!(
                "bench: {} of {} turn(s) could not be measured and are excluded from every \
                 aggregate — see the report's 'Turn measurement coverage' section",
                breakdowns.unmeasurable.len(),
                breakdowns.attempted()
            );
        }
        let turn_taking = report::turn_taking_all(&all_marks, trace_dir, &manifest)?;
        // `--llm-local` is an operator statement, not a guess: the harness
        // has no way to detect whether the server's LLM is co-located (see
        // Args::llm_local's help text), and a wrong auto-detected label
        // would be worse than none.
        // The measured RTT is what makes `network_ms` — a residual, not a
        // measurement — checkable at all (`residual_is_plausible`).
        let markdown = report::render_markdown(
            &breakdowns,
            &category_summaries,
            &turn_taking,
            args.llm_local,
            rtt_probe_ms.as_ref().map(|s| s.p50),
            &failures,
        );
        std::fs::write(out_report, &markdown)
            .with_context(|| format!("writing report to {}", out_report.display()))?;
        println!("Report written to {}", out_report.display());
    }

    let out = std::fs::File::create(&args.out_marks)?;
    serde_json::to_writer_pretty(
        out,
        &BenchOutput {
            rtt_probe_ms,
            failures,
            marks: all_marks,
        },
    )?;

    Ok(())
}

/// Reports a session loop's outcome and returns a failure description when it
/// ended in error.
///
/// `eprintln!`, not `tracing::error!`: no example in this crate installs a
/// `tracing_subscriber`, so the previous `tracing::error!` went nowhere at all
/// and a dead session looked exactly like a server that never answered.
fn loop_failure(repetition: u64, name: &str, res: Result<()>) -> Option<String> {
    match res {
        Ok(()) => None,
        Err(err) => {
            let msg = format!("{name} error: {err}");
            eprintln!("bench: repetition {repetition}: {msg}");
            Some(msg)
        }
    }
}

/// [`loop_failure`] for a loop that was spawned, so a panic or a cancellation
/// also counts as a failure rather than a clean finish.
fn join_failure(
    repetition: u64,
    name: &str,
    res: std::result::Result<Result<()>, tokio::task::JoinError>,
) -> Option<String> {
    match res {
        Ok(inner) => loop_failure(repetition, name, inner),
        Err(join_err) => {
            let msg = format!("{name} task did not finish cleanly: {join_err}");
            eprintln!("bench: repetition {repetition}: {msg}");
            Some(msg)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn e2e_is_first_agent_audio_minus_user_speech_end() {
        let marks = vec![
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::UserSpeechEnd, t_us: 1_000_000, sample_idx: 24000 },
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::FirstAgentAudio, t_us: 1_850_000, sample_idx: 0 },
        ];
        assert_eq!(e2e_ms(&marks, 0, 0), Some(850.0));
    }

    #[test]
    fn e2e_is_none_when_the_agent_never_answered() {
        let marks = vec![ClientMark {
            repetition: 0,
            turn: 0,
            kind: MarkKind::UserSpeechEnd,
            t_us: 1_000_000,
            sample_idx: 24000,
        }];
        assert_eq!(e2e_ms(&marks, 0, 0), None);
    }

    /// The bug this test pins: with a fresh session per repetition, `turn`
    /// and `sample_idx` both restart at zero every repetition, so two
    /// different repetitions' marks can be bit-for-bit identical except for
    /// `repetition` and `t_us`. Matching on `turn` alone (the pre-fix
    /// behavior) would silently pick marks from the wrong repetition —
    /// confident, wrong output. Matching on `(repetition, turn)` must not
    /// conflate them.
    #[test]
    fn e2e_ms_does_not_conflate_marks_from_different_repetitions() {
        let marks = vec![
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::UserSpeechEnd, t_us: 1_000_000, sample_idx: 24_000 },
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::FirstAgentAudio, t_us: 1_850_000, sample_idx: 0 },
            ClientMark { repetition: 1, turn: 0, kind: MarkKind::UserSpeechEnd, t_us: 1_000_000, sample_idx: 24_000 },
            ClientMark { repetition: 1, turn: 0, kind: MarkKind::FirstAgentAudio, t_us: 1_500_000, sample_idx: 0 },
        ];
        assert_eq!(e2e_ms(&marks, 0, 0), Some(850.0), "repetition 0 must not pick up repetition 1's audio mark");
        assert_eq!(e2e_ms(&marks, 1, 0), Some(500.0), "repetition 1 must not pick up repetition 0's audio mark");
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

    /// A session loop that ends in error must produce a failure the report
    /// can render. The bug this pins: the old `pp_err` logged via
    /// `tracing::error!`, no example here installs a subscriber, so the
    /// output went nowhere and `run_session` returned `Ok` regardless — a
    /// dead session was reported as the agent never answering.
    #[test]
    fn a_loop_error_becomes_a_failure_naming_the_loop_and_the_cause() {
        let f = loop_failure(3, "recv_loop", Err(anyhow::anyhow!("Error from server: boom")))
            .expect("an errored loop must produce a failure");
        assert!(f.contains("recv_loop"), "must name which loop died: {f}");
        assert!(f.contains("boom"), "must carry the underlying cause: {f}");
    }

    #[test]
    fn a_clean_loop_produces_no_failure() {
        assert!(
            loop_failure(0, "send_loop", Ok(())).is_none(),
            "a clean run must not be flagged incomplete"
        );
    }

    /// The receive loop is spawned, so "did not return an error" is not the
    /// same as "finished": a panic or a cancellation arrives as a `JoinError`
    /// and must not read as a clean session either.
    #[tokio::test]
    async fn a_join_error_counts_as_a_failure_not_a_clean_finish() {
        let task = tokio::spawn(async { std::future::pending::<Result<()>>().await });
        task.abort();
        let f = join_failure(1, "recv_loop", task.await)
            .expect("a cancelled task must produce a failure");
        assert!(f.contains("recv_loop"), "got: {f}");
    }

    /// `--llm-local` must default to `true` (today's reality: every backend
    /// this harness currently measures is a co-located vLLM) and must accept
    /// an explicit override to `false` — a plain `bool` field with
    /// `default_value_t` alone would be inferred by clap as a presence-only
    /// flag (`ArgAction::SetTrue`) that *cannot* be set to `false` from the
    /// command line at all, silently defeating the whole point of the flag.
    /// This pins the CLI wiring itself, not just `render_markdown`'s
    /// behavior once it has a `bool` in hand.
    #[test]
    fn llm_local_flag_defaults_true_and_accepts_an_explicit_false() {
        let base = ["gradbot-bench", "--url", "ws://x", "--manifest", "m.json", "--out-marks", "o.json"];

        let default_args = Args::try_parse_from(base).unwrap();
        assert!(default_args.llm_local, "must default to true: this harness's LLM is co-located today");

        let explicit_false = Args::try_parse_from(base.iter().chain(["--llm-local", "false"].iter())).unwrap();
        assert!(!explicit_false.llm_local, "--llm-local false must be settable from the CLI");

        let explicit_true = Args::try_parse_from(base.iter().chain(["--llm-local", "true"].iter())).unwrap();
        assert!(explicit_true.llm_local);
    }
}
