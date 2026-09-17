//! Real-time fixture player and client-side latency marks.
//!
//! Streams pre-recorded utterances from a fixture manifest into a live
//! gradbot session at real speed and records two client-side timestamps per
//! turn: when the user's speech ends, and when the agent's first real audio
//! arrives. The gap between them is the number that matters to a user: how
//! long after they stop speaking does the agent start talking.
//!
//! Every session opens with a `session.update` (`send_session_update`,
//! `build_session_config`) before any audio is streamed -- the server
//! rejects audio on an unconfigured session. That also means `assistant_speaks_first: true`
//! (hardcoded server-side, see the `BENCHMARK TRAP` comment in
//! `openai_server.rs`) fires an unsolicited greeting the instant the session
//! is configured, so `drain_greeting` consumes and discards that audio,
//! recording no marks, before playback starts -- otherwise the greeting gets
//! mistaken for turn 0's answer.
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
//! The probe reads from the measured session's own socket, so whatever the
//! server had already sent — starting with the Ogg Opus stream header, which
//! it emits on accept without waiting for any client audio — is captured in
//! `RttProbe::drained` and replayed into the receive loop ahead of the live
//! stream. Discarding it would leave the decoder without a stream header and
//! produce a 100% `missed_endpoint_rate` on repetition 0 of some runs.
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

/// Waits for the pong matching an already-sent ping, **collecting** any other
/// frames that interleave into `drained` rather than discarding them.
///
/// Discarding was a real defect, not a theoretical one: the probe runs on the
/// measured session's own socket, and the server starts sending the instant it
/// accepts the connection (see `probe_rtt_ms`). `drained` is borrowed from the
/// caller rather than returned so that frames already collected survive the
/// `RTT_PING_TIMEOUT` cancelling this future mid-await.
///
/// Factored out of `probe_rtt_ms` so that timeout wraps a single, ordinary
/// future.
async fn wait_for_pong(ws: &mut WebSocket, drained: &mut Vec<ws::Message>) -> Result<()> {
    while let Some(msg) = ws.next().await {
        match msg? {
            ws::Message::Pong(_) => return Ok(()),
            other => drained.push(other),
        }
    }
    anyhow::bail!("connection closed while waiting for a pong")
}

/// The receive loop's frame source: everything the RTT probe drained, in
/// arrival order, before anything newly arriving on the socket.
///
/// Ordering is the whole point. The bench's Opus decoder is stateful and the
/// first frame of a session is the stream header, so a drained frame replayed
/// after a live one — or not at all — is as bad as never receiving it (see
/// [`RttProbe::drained`]).
async fn next_frame(
    replay: &mut std::vec::IntoIter<ws::Message>,
    receiver: &mut Receiver,
) -> Option<Result<ws::Message>> {
    match replay.next() {
        Some(frame) => Some(Ok(frame)),
        None => receiver.next().await,
    }
}

/// What the RTT probe produced: the round-trip summary, and every non-pong
/// frame it took off the socket while waiting for pongs.
pub struct RttProbe {
    pub summary: Summary,
    /// Server frames that arrived during the probe, in arrival order.
    ///
    /// These **must** be handed to the receive loop ahead of anything that
    /// arrives afterwards. They are not spare diagnostics: the very first
    /// frame of a session is the Ogg Opus stream header, and a decoder that
    /// never sees it decodes nothing, forever.
    pub drained: Vec<ws::Message>,
}

/// Measures WebSocket round-trip time with up to `n` ping/pong exchanges. Run
/// once per invocation, on repetition 0's connection before its fixture
/// playback begins, so it never competes with the traffic being measured and
/// never opens a connection of its own (see the module docs: an extra
/// connection means an extra server-side trace file).
///
/// **The server does send frames before it receives any audio**, so the probe
/// must not discard what it reads while waiting for a pong. On accept,
/// `openai_server::realtime` starts a session with `Format::OggOpus`, and the
/// out-send loop's first action is a `MsgOut::Audio` carrying
/// `encoder.header()`, which `msg_out_consumer` forwards as a
/// `response.audio.delta` — no client audio required. Whether that header
/// lands inside the probe's window is a race between the server's STT
/// handshake and ~20 client round trips, both in the tens of milliseconds, so
/// it happens on some runs and not others. Swallowing it leaves
/// `kaudio::ogg_opus::Decoder` without a stream header: at best a decode
/// error, at worst a decoder that returns nothing for the rest of the
/// session — no `FirstAgentAudio` marks and a 100% `missed_endpoint_rate` on
/// repetition 0, reported as "the agent never answered" when the agent
/// answered fine. Every frame read here is therefore returned in
/// [`RttProbe::drained`] for the receive loop to process first.
///
/// A ping that does not get a pong back within `RTT_PING_TIMEOUT` is skipped
/// rather than treated as fatal; only ending up with fewer than two samples
/// overall is an error, since a transport this harness cannot characterize
/// must fail loudly rather than silently produce a bogus baseline. Frames
/// drained before such a miss are still kept.
pub async fn probe_rtt_ms(ws: &mut WebSocket, n: usize) -> Result<RttProbe> {
    use futures_util::SinkExt;
    let mut samples = Vec::with_capacity(n);
    let mut drained = Vec::new();
    for i in 0..n {
        let started = Instant::now();
        ws.send(ws::Message::Ping(vec![i as u8])).await?;
        match tokio::time::timeout(RTT_PING_TIMEOUT, wait_for_pong(ws, &mut drained)).await {
            Ok(Ok(())) => samples.push(started.elapsed().as_secs_f64() * 1000.0),
            Ok(Err(_)) | Err(_) => continue, // read error, close, or timeout: skip this sample
        }
    }
    let summary =
        summarize(&samples).ok_or_else(|| anyhow::anyhow!("rtt probe collected < 2 samples"))?;
    Ok(RttProbe { summary, drained })
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

    /// Optional endpointing overrides, forwarded verbatim in the
    /// `session.update` payload (see `SessionConfig` in `openai_protocol.rs`).
    /// Left unset by default so the field is simply absent from the wire
    /// payload and the server keeps its own hardcoded default -- see
    /// `build_session_config`. Exists so a later sweep task can vary these to
    /// find the latency/quality frontier.
    #[clap(long)]
    flush_duration_s: Option<f64>,

    #[clap(long)]
    min_listen_before_flush_s: Option<f64>,

    #[clap(long)]
    vad_eot_threshold: Option<f64>,

    #[clap(long)]
    padding_bonus: Option<f64>,

    #[clap(long)]
    silence_timeout_s: Option<f64>,
}

/// Builds the `session.update` payload from CLI flags. Only the endpointing
/// fields the operator actually set are included in the struct as `Some`;
/// `SessionConfig`'s `skip_serializing_if = "Option::is_none"` then leaves
/// every unset field entirely absent from the wire payload, so
/// `resolve_endpointing` (server-side) falls back to today's hardcoded
/// default for it. Sending an explicit "default" value instead would
/// silently override that default, which is exactly what must not happen for
/// a field the operator never asked to change.
fn build_session_config(args: &Args) -> oai::SessionConfig {
    oai::SessionConfig {
        instructions: None,
        voice: None,
        voice_id: None,
        allow_recording: false,
        lang: None,
        flush_duration_s: args.flush_duration_s,
        min_listen_before_flush_s: args.min_listen_before_flush_s,
        vad_eot_threshold: args.vad_eot_threshold,
        padding_bonus: args.padding_bonus,
        silence_timeout_s: args.silence_timeout_s,
    }
}

/// Sends the `session.update` client event. Must happen before any audio is
/// streamed: the server rejects audio on an unconfigured session with
/// "Session configuration required before sending audio".
async fn send_session_update(ws: &mut WebSocket, session: oai::SessionConfig) -> Result<()> {
    use futures_util::SinkExt;
    let msg = serde_json::to_string(&oai::ClientEvent::session_update(session))?;
    ws.send(ws::Message::Text(msg)).await?;
    Ok(())
}

/// How long the agent's greeting audio must be quiet before fixture playback
/// begins. `assistant_speaks_first: true` is hardcoded server-side (see the
/// `BENCHMARK TRAP` comment in `openai_server.rs`), so the instant our
/// `session.update` lands the agent starts speaking an unsolicited greeting.
/// If that audio is still arriving when playback starts, it gets recorded as
/// turn 0's `FirstAgentAudio` and turn 0's latency measures the greeting, not
/// any answer -- a small, plausible-looking number that is pure fiction.
const GREETING_QUIET_WINDOW: Duration = Duration::from_millis(1500);

/// Hard cap on how long `drain_greeting` will wait, regardless of whether the
/// greeting ever goes quiet. A server that greets forever must not be allowed
/// to hang the whole benchmark; the run continues (and says so on stderr)
/// rather than measuring nothing.
const GREETING_DRAIN_CAP: Duration = Duration::from_secs(6);

/// Consumes and discards inbound agent audio after the session update until
/// it has been quiet for `GREETING_QUIET_WINDOW`, or `GREETING_DRAIN_CAP` is
/// hit. Records no marks -- this phase must never be mistaken for a fixture
/// turn.
///
/// Deliberately does **not** filter greeting audio out by comparing its
/// arrival time to a turn's `UserSpeechEnd`. That is exactly what a premature
/// cut looks like, and `is_premature_cut` (Task 13's report code) depends on
/// being able to tell the two apart. Draining the greeting in its own phase,
/// before playback starts, keeps them distinguishable; filtering by timing
/// would silently destroy that signal instead.
///
/// There is no explicit "greeting done" event to wait on -- the server never
/// emits `ResponseAudioDone` or `ResponseCreated` -- so a quiet-period
/// heuristic is the only option.
///
/// `decoder` is the same decoder `recv_loop` goes on to use for the rest of
/// the session: the Ogg Opus stream's header is sent exactly once, at the
/// very start of the session, and a decoder created fresh after this phase
/// would never see it.
async fn drain_greeting(
    repetition: u64,
    ws: &mut WebSocket,
    decoder: &mut kaudio::ogg_opus::Decoder,
) -> Result<()> {
    let cap_deadline = Instant::now() + GREETING_DRAIN_CAP;
    let mut quiet_deadline = Instant::now() + GREETING_QUIET_WINDOW;
    loop {
        let now = Instant::now();
        if now >= quiet_deadline {
            return Ok(());
        }
        if now >= cap_deadline {
            eprintln!(
                "bench: repetition {repetition}: greeting drain hit its \
                 {GREETING_DRAIN_CAP:?} cap without going quiet; starting playback anyway"
            );
            return Ok(());
        }
        let wait = quiet_deadline.min(cap_deadline) - now;
        let frame = match tokio::time::timeout(wait, ws.next()).await {
            // Neither deadline had a message to show for it; loop back to
            // the top and re-check which one was actually hit.
            Err(_) => continue,
            Ok(frame) => frame,
        };
        let msg = match frame {
            None => anyhow::bail!("connection closed while draining the greeting"),
            Some(msg) => msg?,
        };
        let event: oai::ServerEvent = match msg {
            ws::Message::Text(t) => serde_json::from_str(&t)?,
            ws::Message::Binary(b) => serde_json::from_slice(&b)?,
            ws::Message::Close(_) => {
                anyhow::bail!("connection closed while draining the greeting")
            }
            ws::Message::Frame(_) | ws::Message::Ping(_) | ws::Message::Pong(_) => continue,
        };
        match event {
            oai::ServerEvent::Error { event_id: _, error } => {
                anyhow::bail!("Error from server while draining the greeting: {error:?}");
            }
            oai::ServerEvent::ResponseAudioDelta { event_id: _, delta } => {
                if let Some(decoded) = decoder.decode(&delta)?
                    && is_agent_audio(decoded)
                {
                    quiet_deadline = Instant::now() + GREETING_QUIET_WINDOW;
                }
            }
            _ => {}
        }
    }
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
    session_config: oai::SessionConfig,
) -> Result<SessionOutcome> {
    let mut connection = Connection::new(url).await?;

    // The server rejects audio on an unconfigured session ("Session
    // configuration required before sending audio"), so this must land
    // before any fixture audio -- and before the RTT probe, which itself
    // sends nothing but does read from this same socket.
    send_session_update(&mut connection.ws, session_config).await?;

    // `assistant_speaks_first: true` is hardcoded server-side, so the agent
    // greets unsolicited the instant the session is configured. This decoder
    // is created here, rather than inside `recv_loop`, because it must be the
    // one and only decoder for the whole session: the Ogg Opus stream's
    // header is sent exactly once, right at the start, and it will be
    // consumed here (whether it turns up before the greeting, during it, or
    // interleaved with the RTT probe below).
    let mut opus_decoder = kaudio::ogg_opus::Decoder::new(OUT_SAMPLE_RATE, 0)?;
    drain_greeting(repetition, &mut connection.ws, &mut opus_decoder).await?;

    // `probe_rtt_ms` reads from the socket, so anything the server sends
    // during the probe comes back in `drained` and must be replayed into the
    // receive loop before any newly-arriving frame.
    let (rtt_probe_ms, drained) = match rtt_probe_pings {
        Some(n) => {
            let probe = probe_rtt_ms(&mut connection.ws, n).await?;
            (Some(probe.summary), probe.drained)
        }
        None => (None, Vec::new()),
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
            //
            // Continues the decoder `drain_greeting` already used, rather
            // than starting a fresh one: the Ogg Opus stream header was fed
            // to it back then, and a new decoder here would never see it.
            let mut opus_decoder = opus_decoder;
            let mut last_marked_turn: Option<u64> = None;
            // Frames the RTT probe took off the socket come first, in arrival
            // order, and only then the live stream. The decoder is stateful:
            // replaying out of order, or not at all, is what loses the Ogg
            // stream header.
            let mut replay = drained.into_iter();
            while let Some(frame) = next_frame(&mut replay, &mut receiver).await {
                let msg: oai::ServerEvent = match frame? {
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
    let session_config = build_session_config(&args);

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
        let outcome = run_session(
            &args.url,
            &manifest,
            rep as u64,
            probe_pings,
            session_config.clone(),
        )
        .await?;
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

    /// The regression this pins: the RTT probe runs on the *measured*
    /// session's socket, and the server sends the Ogg Opus stream header the
    /// moment it accepts — `openai_server::realtime` starts a session with
    /// `Format::OggOpus` and the out-send loop's first act is a `MsgOut::Audio`
    /// carrying `encoder.header()`, forwarded as a `response.audio.delta` with
    /// no client audio required. `wait_for_pong` used to discard every
    /// non-pong frame for the whole 20-ping probe, so whether the header
    /// survived was a race. Losing it leaves `kaudio::ogg_opus::Decoder`
    /// without a stream header: no `FirstAgentAudio` marks and a 100%
    /// `missed_endpoint_rate` on repetition 0, read as "the agent never
    /// answered" when the agent answered fine.
    ///
    /// Drives a real loopback WebSocket rather than a mock, because the thing
    /// under test is exactly the interleaving of server frames with ping/pong
    /// on one socket.
    #[tokio::test]
    async fn frames_arriving_during_the_probe_reach_the_receive_path_before_live_ones() {
        use futures_util::SinkExt;

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();

        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut ws = tokio_tungstenite::accept_async(stream).await.unwrap();
            // Sent on accept, before any client audio — the stream header.
            ws.send(ws::Message::Text(
                r#"{"type":"response.audio.delta","delta":"HEADER"}"#.to_string(),
            ))
            .await
            .unwrap();
            // Keep polling so tungstenite answers the probe's pings; when the
            // client signals it is past the probe, send one more frame.
            while let Some(Ok(msg)) = ws.next().await {
                match msg {
                    ws::Message::Text(t) if t == "go" => {
                        ws.send(ws::Message::Text(
                            r#"{"type":"response.audio.delta","delta":"LIVE"}"#.to_string(),
                        ))
                        .await
                        .unwrap();
                    }
                    ws::Message::Close(_) => break,
                    _ => {}
                }
            }
        });

        let mut connection = Connection::new(&format!("ws://{addr}")).await.unwrap();
        let probe = probe_rtt_ms(&mut connection.ws, 4).await.unwrap();
        assert!(probe.summary.n >= 2, "probe needs samples to be meaningful");

        // Handed back, not discarded.
        let header_drained = probe.drained.iter().any(
            |m| matches!(m, ws::Message::Text(t) if t.contains("HEADER")),
        );
        assert!(
            header_drained,
            "the header frame must survive the probe: {:?}",
            probe.drained
        );

        // And it reaches the receive path ahead of anything arriving later.
        let (mut sender, mut receiver) = connection.split();
        let mut replay = probe.drained.into_iter();
        sender
            .ws_sender
            .send(ws::Message::Text("go".to_string()))
            .await
            .unwrap();

        let first = next_frame(&mut replay, &mut receiver).await.unwrap().unwrap();
        assert!(
            matches!(&first, ws::Message::Text(t) if t.contains("HEADER")),
            "the drained header must be replayed first, before the live frame: {first:?}"
        );
        let second = loop {
            let f = next_frame(&mut replay, &mut receiver).await.unwrap().unwrap();
            if let ws::Message::Text(t) = &f
                && t.contains("LIVE")
            {
                break f;
            }
        };
        assert!(matches!(second, ws::Message::Text(_)));

        drop(receiver);
        drop(sender);
        server.abort();
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

    /// The endpointing fields the operator set must be present in the
    /// `session.update` payload with their given values -- and just as
    /// importantly, every field the operator did *not* set must be entirely
    /// absent from the serialized payload, not present with some "default"
    /// value. `resolve_endpointing` (server-side) only falls back to today's
    /// hardcoded default when a field is *missing*; a field sent as an
    /// explicit value, even one that happens to match the current default,
    /// would silently pin that value on the wire and defeat the whole point
    /// of leaving it unset.
    #[test]
    fn session_update_payload_carries_only_the_endpointing_flags_the_operator_set() {
        let base = [
            "gradbot-bench",
            "--url",
            "ws://x",
            "--manifest",
            "m.json",
            "--out-marks",
            "o.json",
            "--vad-eot-threshold",
            "0.42",
            "--silence-timeout-s",
            "9.5",
        ];
        let args = Args::try_parse_from(base).unwrap();
        let session = build_session_config(&args);
        let json = serde_json::to_value(&session).unwrap();
        let obj = json.as_object().expect("session config serializes to an object");

        assert_eq!(obj.get("vad_eot_threshold"), Some(&serde_json::json!(0.42)));
        assert_eq!(obj.get("silence_timeout_s"), Some(&serde_json::json!(9.5)));

        // Not set on the CLI -- must not appear in the payload at all.
        assert!(
            !obj.contains_key("flush_duration_s"),
            "unset field leaked into the payload: {obj:?}"
        );
        assert!(
            !obj.contains_key("min_listen_before_flush_s"),
            "unset field leaked into the payload: {obj:?}"
        );
        assert!(
            !obj.contains_key("padding_bonus"),
            "unset field leaked into the payload: {obj:?}"
        );
    }

    /// When the operator sets none of the five endpointing flags, none of
    /// them may appear in the payload -- the server must see exactly what a
    /// plain `session.update` without endpointing overrides has always sent.
    #[test]
    fn session_update_payload_omits_all_endpointing_fields_by_default() {
        let base = ["gradbot-bench", "--url", "ws://x", "--manifest", "m.json", "--out-marks", "o.json"];
        let args = Args::try_parse_from(base).unwrap();
        let session = build_session_config(&args);
        let json = serde_json::to_value(&session).unwrap();
        let obj = json.as_object().expect("session config serializes to an object");

        for field in [
            "flush_duration_s",
            "min_listen_before_flush_s",
            "vad_eot_threshold",
            "padding_bonus",
            "silence_timeout_s",
        ] {
            assert!(!obj.contains_key(field), "{field} must be absent by default: {obj:?}");
        }
    }
}
