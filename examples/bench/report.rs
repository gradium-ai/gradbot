//! Joins client-observed marks to the server's internal trace and
//! decomposes one turn's end-to-end latency into attributable stages.
//!
//! No clock synchronisation is attempted or needed: the client and the
//! server each keep their own monotonic `Instant` origin, and those origins
//! are unrelated. Both sides only ever produce *durations* on their own
//! clock. The two duration series are joined on a quantity both genuinely
//! share — the cumulative input sample index — and causal ordering (by
//! timestamp, within one side's own clock) does the rest.
//!
//! Two rules that make the join correct rather than merely plausible:
//!
//! - **Never join by turn equality.** The client's `turn` counts fixture
//!   turns; the server's `TraceRecord::turn` is its own sequence that also
//!   advances on interruptions, and the wire protocol carries no turn id for
//!   the client to adopt. `breakdown` takes a `turn` only to select which of
//!   the *client's* marks to use — every server-side anchor (`I`, `V`, `O`)
//!   is found by sample index and time, never by comparing to that `turn`.
//! - **A missing anchor is an error, never a zero.** Real traces legitimately
//!   contain unclosed spans (e.g. `endpoint.flush` abandoned by a mid-flush
//!   interruption). Substituting 0 would understate latency for exactly the
//!   turns that went wrong.
//! - **A forward search stops at the next turn.** `find_v` and `find_o`
//!   search forward from `I`; on a premature cut this turn's own records
//!   precede `I`, and an unbounded search would return the next turn's
//!   instead — plausible numbers, wrong turn, no error anywhere. Both are
//!   bounded by the next turn's `I` (`next_turn_ceiling_t_us`), and hitting
//!   the bound yields "not measured", never a borrowed value. What survives
//!   that bound is caught by `residual_is_plausible`, checked per turn in
//!   `render_markdown` against the run's own RTT probe.
//!
//! Repetition scoping happens one level up, in [`breakdown_all`], before
//! `breakdown` itself is ever called: `--repetitions N` opens N fresh
//! sessions, so both `ClientMark.turn` and `TraceRecord.sample_idx` restart
//! at zero every repetition (a new session restarts the server's sample
//! counter too). `breakdown`'s own `marks` and `trace` arguments must already
//! be scoped to a single repetition — `breakdown_all` pairs repetition *i*'s
//! marks with the *i*-th trace file in timestamp order (valid because the
//! harness runs exactly one session at a time). Joining across repetitions
//! would pair the wrong marks with the wrong trace and produce confident,
//! wrong numbers with no error anywhere — `breakdown` itself has no way to
//! detect this, since both join keys look identical across repetitions. This
//! is why `breakdown_all` asserts the discovered trace-file count matches the
//! repetition count found in `marks` and fails loudly rather than zipping the
//! two lists to whichever is shorter.

use super::fixtures::Manifest;
use super::{ClientMark, MarkKind, RunFailure, Summary, residual_is_plausible, summarize};
use anyhow::{Context, Result};
use gradbot::{Phase, TraceRecord};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

/// One turn's latency, decomposed into stages, plus the spans that make up
/// `server_internal_ms` for a Gantt-style render.
#[derive(Debug, Clone, PartialEq)]
pub struct TurnBreakdown {
    /// Which repetition's session this turn came from. Carried alongside
    /// `turn` because `turn` alone does not identify a turn across a
    /// multi-repetition run — it restarts at zero every repetition (see
    /// [`ClientMark::repetition`]) — and a report bullet that cannot be
    /// traced back to one specific turn is not actionable.
    pub repetition: u64,
    pub turn: u64,
    /// First agent audio minus user speech end, on the client's own clock.
    pub e2e_ms: f64,
    /// `V - I`: how long the server took to detect end-of-turn after the
    /// sample the client marked as speech end. `None` when no
    /// `endpoint.vad_eot` was found for this turn — either because the trace
    /// genuinely carries no such event (`find_v` is a soft anchor) or because
    /// the only one in range belongs to the next turn (see
    /// `next_turn_ceiling_t_us`). This is `Option`,
    /// not a sentinel float, on purpose: a `NaN` would silently poison any
    /// downstream sum/average and panic this codebase's `percentile`
    /// (`partial_cmp().expect(...)`), while a fake `0.0` would look like a
    /// real, implausibly instant detection. Do not "simplify" this back to
    /// `f64` — `None` is the only representation that can't be mistaken for
    /// a measurement.
    pub detection_lag_ms: Option<f64>,
    /// `O - I`: everything the server did between receiving that sample and
    /// emitting first agent audio.
    pub server_internal_ms: f64,
    /// `e2e_ms - server_internal_ms`: the residual left over once the
    /// server's own accounting is subtracted from what the client observed —
    /// transport plus anything the server doesn't instrument.
    pub network_ms: f64,
    /// `(span name, start_ms, end_ms)`, relative to `I`, one entry per
    /// closed Begin/End pair recorded under the server turn that produced
    /// **`O`** — not the one that produced `I`. `audio_in.frame` (which
    /// supplies `I`) is emitted from the input loop, which has no per-turn
    /// context and hardcodes `turn: 0` (`multiplex.rs`); selecting on `I`'s
    /// turn therefore rendered turn 0's spans under every turn, positioned
    /// against a much later `I` and collapsing to 1-char bars. `O`
    /// (`out.first_audio`) is emitted from the out-send loop with the
    /// server's real `turn_idx`, so it is the only anchor here that carries
    /// one. Offsets stay relative to `i.t_us`.
    ///
    /// Spans left open (e.g. an abandoned `endpoint.flush`) are omitted
    /// here — they have no end to draw — but never cause `breakdown` itself
    /// to fail; only a missing `I`, `V`, or `O` anchor does that.
    pub spans: Vec<(String, f64, f64)>,
}

/// One turn that could not be decomposed at all, and why.
///
/// Recorded rather than propagated: a turn is unmeasurable for reasons local
/// to that turn (most often a premature cut, where the server endpointed
/// before the client's marked speech end and the turn's own `O` therefore
/// falls outside the window — see `next_turn_ceiling_t_us`). Aborting the
/// whole report for one such turn throws away every other turn in the run and
/// makes the endpointing sweep impossible: the sweep drives
/// `flush_duration_s` / `min_listen_before_flush_s` down *until* premature
/// cuts appear, so every sweep point aggressive enough to be interesting
/// would produce no output at all — and the Pareto operating point sits
/// exactly in that region.
///
/// The correctness rule is unchanged: an unmeasurable turn still contributes
/// **no number anywhere**. Only the blast radius shrank, from the whole report
/// to the single turn.
#[derive(Debug, Clone, PartialEq)]
pub struct TurnFailure {
    /// Which repetition's session this turn belongs to.
    pub repetition: u64,
    /// The client's per-repetition turn index — see [`ClientMark::turn`].
    pub turn: u64,
    /// The full error chain from [`breakdown`], rendered with `{:#}`.
    pub reason: String,
}

/// Everything [`breakdown_all`] produced: the turns that decomposed cleanly,
/// and the ones that could not be measured.
///
/// Both halves travel together on purpose. A caller handed only `measured`
/// cannot tell 19-of-20 from 20-of-20, and every aggregate the report draws
/// from `measured` has to be able to state what it excluded.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Breakdowns {
    pub measured: Vec<TurnBreakdown>,
    pub unmeasurable: Vec<TurnFailure>,
}

impl Breakdowns {
    /// Turns attempted: measured plus unmeasurable. The denominator every
    /// aggregate over `measured` must state itself against.
    pub fn attempted(&self) -> usize {
        self.measured.len() + self.unmeasurable.len()
    }
}

/// Finds the client's `kind` mark for `turn`, or errors naming what's missing.
fn client_mark(marks: &[ClientMark], turn: u64, kind: MarkKind) -> Result<&ClientMark> {
    marks
        .iter()
        .find(|m| m.turn == turn && m.kind == kind)
        .with_context(|| format!("turn {turn}: missing client mark {kind:?}"))
}

/// `I`: the first `audio_in.frame` whose `sample_idx` has reached
/// `target_sample_idx`. Frames advance in fixed-size steps, so an exact
/// match on `sample_idx` is not guaranteed — this is why `>=` rather than
/// `==`.
fn find_i(trace: &[TraceRecord], target_sample_idx: u64) -> Result<&TraceRecord> {
    trace
        .iter()
        .filter(|r| r.span == "audio_in.frame" && r.sample_idx >= target_sample_idx)
        .min_by_key(|r| r.t_us)
        .context("missing span: audio_in.frame (I) — no frame reached the marked sample index")
}

/// Exclusive upper bound on a forward search, or `None` for "no next turn to
/// confuse this one with" — see [`next_turn_ceiling_t_us`].
type Ceiling = Option<u64>;

/// Whether `r` falls in `[t_us_floor, ceiling)`.
fn within(r: &TraceRecord, t_us_floor: u64, ceiling: Ceiling) -> bool {
    r.t_us >= t_us_floor && ceiling.is_none_or(|c| r.t_us < c)
}

/// The trace timestamp where the *next* fixture turn's `I` lands, used as an
/// exclusive ceiling on the forward searches below.
///
/// Both `find_v` and `find_o` search forward from `I`. On a **premature cut**
/// — the server endpointed before the client's marked speech end — this
/// turn's real `endpoint.vad_eot` and `out.first_audio` both *precede* `I`,
/// so an unbounded forward search silently returns the NEXT turn's records:
/// `detection_lag_ms` and `server_internal_ms` become another turn's numbers,
/// `e2e_ms - server_internal_ms` goes negative, and `network_ms` is printed
/// in the percentile table as a plain number. Nothing downstream can tell.
///
/// `None` when `turn` is the last turn in `marks` (there is no later turn to
/// borrow from) or when the next turn's own `I` is not in the trace.
///
/// This bound is necessary, not sufficient: if the *next* turn is itself
/// prematurely cut, its `O` also precedes its `I` and lands inside this
/// window. That residue is what `residual_is_plausible` is checked for in
/// `render_markdown`.
fn next_turn_ceiling_t_us(marks: &[ClientMark], trace: &[TraceRecord], turn: u64) -> Ceiling {
    let next = marks
        .iter()
        .filter(|m| m.kind == MarkKind::UserSpeechEnd && m.turn > turn)
        .min_by_key(|m| m.turn)?;
    find_i(trace, next.sample_idx).ok().map(|r| r.t_us)
}

/// `V`: the first `endpoint.vad_eot` in `[t_us_floor, ceiling)` — by time, not
/// by turn. Unlike `I` and `O`, a missing `V` does not fail the whole
/// breakdown: some traces genuinely carry no endpoint-detection event for a
/// turn (see `breakdown`'s handling of `None` here), and `server_internal_ms`
/// — the number this profiler exists to produce — does not depend on it.
/// "Not found before the next turn began" returns the same `None` as "not
/// recorded at all": both mean *not measured*, and neither may be filled in
/// with a value belonging to another turn.
fn find_v(trace: &[TraceRecord], t_us_floor: u64, ceiling: Ceiling) -> Option<&TraceRecord> {
    trace
        .iter()
        .filter(|r| r.span == "endpoint.vad_eot" && within(r, t_us_floor, ceiling))
        .min_by_key(|r| r.t_us)
}

/// `O`: the first `out.first_audio` in `[t_us_floor, ceiling)` — by time, not
/// by turn. Running past `ceiling` would hand this turn the next turn's first
/// audio, so hitting the bound is an error, never the next turn's record.
fn find_o(trace: &[TraceRecord], t_us_floor: u64, ceiling: Ceiling) -> Result<&TraceRecord> {
    let found = trace
        .iter()
        .filter(|r| r.span == "out.first_audio" && within(r, t_us_floor, ceiling))
        .min_by_key(|r| r.t_us);
    match (found, ceiling) {
        (Some(o), _) => Ok(o),
        (None, None) => anyhow::bail!("missing span: out.first_audio (O)"),
        (None, Some(c)) => anyhow::bail!(
            "missing span: out.first_audio (O) — none in [{t_us_floor}us, {c}us), the window \
             between this turn's marked speech end and the next turn's. There is an \
             `out.first_audio` later in the trace, but it belongs to the next turn and using \
             it would report that turn's latency as this one's. The usual cause is a premature \
             cut: the server endpointed before the client's marked speech end, so this turn's \
             own `out.first_audio` precedes `I` and is outside the window. See \
             `premature_cut_rate` in the turn-taking section."
        ),
    }
}

/// Every closed Begin/End pair recorded under `server_turn` — which must be
/// a *real* server turn, i.e. one taken from a record the server stamped with
/// its own `turn_idx`. `audio_in.frame` is not such a record: its producer
/// hardcodes `turn: 0`. As
/// `(span name, start_ms, end_ms)` relative to `i_t_us`. Pairing is FIFO per
/// span name in time order, so repeated spans of the same name (e.g. a
/// retried `tts.connect`) pair with their own end rather than each other's.
/// A Begin with no matching End (an abandoned span) is left open and simply
/// omitted — it has no end to draw, but that is not an error for this
/// turn's overall breakdown.
fn collect_spans(trace: &[TraceRecord], server_turn: u64, i_t_us: u64) -> Vec<(String, f64, f64)> {
    let mut same_turn: Vec<&TraceRecord> = trace.iter().filter(|r| r.turn == server_turn).collect();
    same_turn.sort_by_key(|r| r.t_us);

    let mut open: std::collections::HashMap<String, Vec<u64>> = std::collections::HashMap::new();
    let mut spans = Vec::new();
    for rec in same_turn {
        match rec.phase {
            Phase::Begin => open.entry(rec.span.clone()).or_default().push(rec.t_us),
            Phase::End => {
                if let Some(starts) = open.get_mut(&rec.span) {
                    if !starts.is_empty() {
                        let start_t_us = starts.remove(0);
                        let start_ms = (start_t_us as f64 - i_t_us as f64) / 1000.0;
                        let end_ms = (rec.t_us as f64 - i_t_us as f64) / 1000.0;
                        spans.push((rec.span.clone(), start_ms, end_ms));
                    }
                }
            }
            Phase::Point => {}
        }
    }
    spans
}

/// Decomposes turn `turn`'s end-to-end latency into stages, joining the
/// client's marks to the server's trace by sample index rather than by
/// turn. `marks` and `trace` must already be scoped to a single repetition
/// (see module docs) — this function has no way to detect a cross-repetition
/// mismatch on its own, since both `turn` and `sample_idx` reset every
/// repetition and would look identical.
pub fn breakdown(marks: &[ClientMark], trace: &[TraceRecord], turn: u64) -> Result<TurnBreakdown> {
    let user_speech_end = client_mark(marks, turn, MarkKind::UserSpeechEnd)?;
    let first_agent_audio = client_mark(marks, turn, MarkKind::FirstAgentAudio)?;
    let e2e_ms = (first_agent_audio.t_us as f64 - user_speech_end.t_us as f64) / 1000.0;

    let i = find_i(trace, user_speech_end.sample_idx)?;
    // Both forward searches stop at the next turn's `I`: past that point every
    // candidate belongs to the next turn (see `next_turn_ceiling_t_us`).
    let ceiling = next_turn_ceiling_t_us(marks, trace, turn);
    let v = find_v(trace, i.t_us, ceiling);
    // `O` follows `V` when `V` was found (the normal case: this also guards
    // against picking up a stale `out.first_audio` from before the endpoint
    // was actually detected). When no `endpoint.vad_eot` exists for this
    // turn, fall back to searching from `I` — `O` must not become
    // unmeasurable just because the detection-lag diagnostic is unavailable.
    let o = find_o(trace, v.map_or(i.t_us, |v| v.t_us), ceiling)?;

    // A missing `V` is "not measured", not "zero lag" (see `find_v` and the
    // field doc on `TurnBreakdown::detection_lag_ms`): `None` makes that
    // explicit rather than smuggling it through as a number.
    let detection_lag_ms = v.map(|v| (v.t_us as f64 - i.t_us as f64) / 1000.0);
    let server_internal_ms = (o.t_us as f64 - i.t_us as f64) / 1000.0;
    let network_ms = e2e_ms - server_internal_ms;

    // Keyed on `O`'s turn, never `I`'s: `audio_in.frame` carries a
    // hardcoded `turn: 0` (see `TurnBreakdown::spans`), so `i.turn` would
    // select turn 0's spans for every turn in the session.
    let spans = collect_spans(trace, o.turn, i.t_us);

    Ok(TurnBreakdown {
        // From the mark, not a parameter: `marks` is already scoped to one
        // repetition (see this function's docs), so the mark is authoritative.
        repetition: user_speech_end.repetition,
        turn,
        e2e_ms,
        detection_lag_ms,
        server_internal_ms,
        network_ms,
        spans,
    })
}

/// Extracts the `<unix_nanos>` embedded in a `trace_<unix_nanos>_<counter>.jsonl`
/// filename, as written by the server (see `gradbot_server::server` and
/// `openai_server`). Parsed from the name itself, never from filesystem
/// metadata: mtime can be rewritten by anything that touches the file (a
/// backup, a `cp -p`, an editor) and is not the ordering the pairing
/// invariant is defined against.
fn trace_timestamp(path: &Path) -> Result<u128> {
    let name = path
        .file_name()
        .and_then(|n| n.to_str())
        .with_context(|| format!("trace file name is not valid UTF-8: {}", path.display()))?;
    let rest = name
        .strip_prefix("trace_")
        .with_context(|| format!("{name}: does not start with 'trace_'"))?;
    let ts_str = rest
        .split('_')
        .next()
        .filter(|s| !s.is_empty())
        .with_context(|| format!("{name}: no timestamp segment after 'trace_'"))?;
    ts_str
        .parse::<u128>()
        .with_context(|| format!("{name}: timestamp segment {ts_str:?} is not a number"))
}

/// Every `trace_*.jsonl` file directly inside `trace_dir`, sorted ascending
/// by the unix-nanosecond timestamp embedded in the filename (see
/// `trace_timestamp`) — the server writes one such file per session, and
/// timestamp order is session order because the harness never runs two
/// sessions concurrently (see module docs).
pub fn discover_trace_files(trace_dir: &Path) -> Result<Vec<PathBuf>> {
    let entries = std::fs::read_dir(trace_dir)
        .with_context(|| format!("reading trace dir {}", trace_dir.display()))?;
    let mut files = Vec::new();
    for entry in entries {
        let path = entry
            .with_context(|| format!("reading trace dir {}", trace_dir.display()))?
            .path();
        let is_trace_file = path
            .file_name()
            .and_then(|n| n.to_str())
            .is_some_and(|n| n.starts_with("trace_") && n.ends_with(".jsonl"));
        if !is_trace_file {
            continue;
        }
        let ts = trace_timestamp(&path)?;
        files.push((ts, path));
    }
    files.sort_by_key(|(ts, _)| *ts);
    Ok(files.into_iter().map(|(_, path)| path).collect())
}

/// Parses one server trace file into its records, in file (write) order.
pub fn load_trace_file(path: &Path) -> Result<Vec<TraceRecord>> {
    let body = std::fs::read_to_string(path)
        .with_context(|| format!("reading trace file {}", path.display()))?;
    body.lines()
        .filter(|line| !line.trim().is_empty())
        .map(|line| {
            serde_json::from_str(line)
                .with_context(|| format!("parsing trace record in {}: {line}", path.display()))
        })
        .collect()
}

/// Pairs every repetition present in `marks` with the corresponding trace
/// file in `trace_dir` and runs [`breakdown`] for every turn of every
/// repetition. See module docs: repetition *i*'s marks pair with the *i*-th
/// trace file in timestamp order.
///
/// **Fails loudly, on purpose, rather than truncating or zip-shortening**,
/// when the discovered trace-file count does not match the number of
/// distinct repetitions found in `marks`. `breakdown` cannot detect a
/// mis-pairing after the fact — both `ClientMark.turn` and
/// `TraceRecord.sample_idx` reset to the same values every repetition, so
/// pairing repetition 3's marks with repetition 7's trace would still find
/// plausible anchors and return confident, wrong numbers. A count mismatch is
/// the only signal available before that damage is done, so it is treated as
/// fatal rather than best-effort.
///
/// A *per-turn* failure is a different animal and is **not** fatal: it is
/// recorded in [`Breakdowns::unmeasurable`] and the remaining turns are
/// returned normally (see [`TurnFailure`] for why one cut turn must not cost
/// a 20-repetition run its other 19). The distinction is deliberate — a
/// count mismatch is a whole-run configuration error that makes every number
/// suspect; one turn being cut makes exactly that turn's numbers absent.
pub fn breakdown_all(marks: &[ClientMark], trace_dir: &Path) -> Result<Breakdowns> {
    let trace_files = discover_trace_files(trace_dir)?;
    let repetitions: BTreeSet<u64> = marks.iter().map(|m| m.repetition).collect();

    anyhow::ensure!(
        trace_files.len() == repetitions.len(),
        "trace-file/repetition count mismatch: found {} trace file(s) in {} but the \
         marks cover {} repetition(s). Refusing to pair them: breakdown cannot detect a \
         mis-pairing on its own (turn and sample_idx both reset every repetition), so a \
         wrong pairing here would silently produce plausible, wrong latency numbers. Check \
         that --trace-dir points at the trace_dir the server was actually configured with, \
         and that no trace files from an earlier run are mixed in.",
        trace_files.len(),
        trace_dir.display(),
        repetitions.len(),
    );

    let mut out = Breakdowns::default();
    for (repetition, trace_path) in repetitions.into_iter().zip(trace_files.iter()) {
        // A trace file that will not load is a whole-run problem (wrong
        // --trace-dir, a truncated write), not one turn's: still fatal.
        let trace = load_trace_file(trace_path)?;
        let rep_marks: Vec<ClientMark> = marks
            .iter()
            .filter(|m| m.repetition == repetition)
            .cloned()
            .collect();
        let turns: BTreeSet<u64> = rep_marks.iter().map(|m| m.turn).collect();
        for turn in turns {
            let attempt = breakdown(&rep_marks, &trace, turn).with_context(|| {
                format!("trace file {}", trace_path.display())
            });
            match attempt {
                Ok(b) => out.measured.push(b),
                // `{:#}` keeps the whole chain — which anchor was missing and
                // the window it was missing from, not just the outermost
                // sentence.
                Err(err) => out.unmeasurable.push(TurnFailure {
                    repetition,
                    turn,
                    reason: format!("{err:#}"),
                }),
            }
        }
    }
    Ok(out)
}

/// Turn-taking quality metrics: how often the agent cut the user off, how
/// long the server took to notice the user was done, and how often it never
/// noticed at all. These get *worse* as endpointing is tuned more
/// aggressively, which is why they must be read alongside latency — a lower
/// `min_listen_before_flush_s` or `vad_eot_threshold` (see `SessionConfig`)
/// can buy a better `e2e_ms` at the cost of these.
#[derive(Debug, Clone)]
pub struct TurnTaking {
    /// Fraction of manifest turns where the agent's first audio arrived
    /// before the user's ground-truth speech end — the agent talked over the
    /// user. A turn with no agent audio at all is excluded here: that is a
    /// missed endpoint, the opposite failure (see `is_premature_cut`).
    pub premature_cut_rate: f64,
    /// `V` (`endpoint.vad_eot`) minus the trace record whose `sample_idx`
    /// matches each turn's `UserSpeechEnd` mark, summarized across every
    /// turn with both anchors. `None` when fewer than two turns have a
    /// measurement — same convention as `summarize`, and for the same
    /// reason `detection_lag_ms` is `Option`, not a sentinel.
    pub endpoint_delay_ms: Option<Summary>,
    /// Fraction of manifest turns with no `FirstAgentAudio` mark at all —
    /// the agent never answered.
    pub missed_endpoint_rate: f64,
}

/// True when the agent's `FirstAgentAudio` mark for `(repetition, turn)`
/// arrived strictly before the user's `UserSpeechEnd` mark for the same
/// `(repetition, turn)` — the agent started talking over the user before
/// they were done. Keyed on `(repetition, turn)`, not `turn` alone: `turn`
/// resets every repetition (see module docs and `e2e_ms`), so matching on
/// it alone would silently pick up marks from the wrong session.
///
/// A turn with no `FirstAgentAudio` mark at all is a *missed* endpoint, not
/// a premature cut — the opposite failure mode — so this returns `false`
/// for it, same as a turn missing its `UserSpeechEnd` mark.
fn is_premature_cut(marks: &[ClientMark], repetition: u64, turn: u64) -> bool {
    let user_speech_end = marks.iter().find(|m| {
        m.repetition == repetition && m.turn == turn && m.kind == MarkKind::UserSpeechEnd
    });
    let first_agent_audio = marks.iter().find(|m| {
        m.repetition == repetition && m.turn == turn && m.kind == MarkKind::FirstAgentAudio
    });
    match (user_speech_end, first_agent_audio) {
        (Some(end), Some(audio)) => audio.t_us < end.t_us,
        _ => false,
    }
}

fn rate(count: u64, total: u64) -> f64 {
    if total == 0 { 0.0 } else { count as f64 / total as f64 }
}

/// Raw counts behind [`TurnTaking`], shared by [`turn_taking`] (one
/// repetition) and [`turn_taking_all`] (pooled across repetitions) so a
/// multi-repetition run's `endpoint_delay_ms` is one percentile over every
/// turn's delay, not an average of per-repetition percentiles.
struct TurnTakingCounts {
    premature_cuts: u64,
    missed: u64,
    total_turns: u64,
    delays_ms: Vec<f64>,
}

fn turn_taking_counts(
    marks: &[ClientMark],
    trace: &[TraceRecord],
    manifest: &Manifest,
) -> TurnTakingCounts {
    let repetition = marks.first().map(|m| m.repetition).unwrap_or(0);
    let mut counts = TurnTakingCounts {
        premature_cuts: 0,
        missed: 0,
        total_turns: manifest.turns.len() as u64,
        delays_ms: Vec::new(),
    };

    for turn in 0..counts.total_turns {
        if is_premature_cut(marks, repetition, turn) {
            counts.premature_cuts += 1;
        }
        let has_agent_audio = marks.iter().any(|m| {
            m.repetition == repetition && m.turn == turn && m.kind == MarkKind::FirstAgentAudio
        });
        if !has_agent_audio {
            counts.missed += 1;
        }
        let user_speech_end = marks.iter().find(|m| {
            m.repetition == repetition && m.turn == turn && m.kind == MarkKind::UserSpeechEnd
        });
        if let Some(end) = user_speech_end
            && let Ok(i) = find_i(trace, end.sample_idx)
            && let Some(v) = find_v(trace, i.t_us, next_turn_ceiling_t_us(marks, trace, turn))
        {
            counts.delays_ms.push((v.t_us as f64 - i.t_us as f64) / 1000.0);
        }
    }
    counts
}

/// Turn-taking quality metrics for one repetition's `marks` against its own
/// `trace` — same single-repetition scoping as [`breakdown`] (see module
/// docs) — with `manifest` supplying the full set of turns that were
/// supposed to happen, so a turn with *no* marks at all (not just no
/// `FirstAgentAudio`) still counts toward `missed_endpoint_rate`.
pub fn turn_taking(marks: &[ClientMark], trace: &[TraceRecord], manifest: &Manifest) -> TurnTaking {
    let c = turn_taking_counts(marks, trace, manifest);
    TurnTaking {
        premature_cut_rate: rate(c.premature_cuts, c.total_turns),
        endpoint_delay_ms: summarize(&c.delays_ms),
        missed_endpoint_rate: rate(c.missed, c.total_turns),
    }
}

/// [`turn_taking`], pooled across every repetition, pairing repetition *i*'s
/// marks with the *i*-th trace file exactly as [`breakdown_all`] does (see
/// its docs for why a trace-file/repetition count mismatch is fatal rather
/// than best-effort here too).
pub fn turn_taking_all(marks: &[ClientMark], trace_dir: &Path, manifest: &Manifest) -> Result<TurnTaking> {
    let trace_files = discover_trace_files(trace_dir)?;
    let repetitions: BTreeSet<u64> = marks.iter().map(|m| m.repetition).collect();

    anyhow::ensure!(
        trace_files.len() == repetitions.len(),
        "trace-file/repetition count mismatch: found {} trace file(s) in {} but the \
         marks cover {} repetition(s). Refusing to pair them — see breakdown_all's error \
         for why a mis-pairing here would silently produce plausible, wrong numbers.",
        trace_files.len(),
        trace_dir.display(),
        repetitions.len(),
    );

    let mut premature_cuts = 0u64;
    let mut missed = 0u64;
    let mut total_turns = 0u64;
    let mut delays_ms = Vec::new();

    for (repetition, trace_path) in repetitions.into_iter().zip(trace_files.iter()) {
        let trace = load_trace_file(trace_path)?;
        let rep_marks: Vec<ClientMark> = marks
            .iter()
            .filter(|m| m.repetition == repetition)
            .cloned()
            .collect();
        let rep_total = manifest.turns.len() as u64;
        // `turn_taking` (rather than reaching for `turn_taking_counts`
        // directly here) is the per-repetition unit this function pools —
        // its rates convert back to exact counts since every repetition
        // shares the same `rep_total` denominator.
        let rep = turn_taking(&rep_marks, &trace, manifest);
        premature_cuts += (rep.premature_cut_rate * rep_total as f64).round() as u64;
        missed += (rep.missed_endpoint_rate * rep_total as f64).round() as u64;
        total_turns += rep_total;
        // Percentiles don't compose across repetitions' already-summarized
        // `Summary`s, so the raw per-turn delays are pooled here, once,
        // straight from the shared counting helper, and summarized only at
        // the end — same principle as `render_markdown`'s
        // `detection_lag_ms` pooling.
        delays_ms.extend(turn_taking_counts(&rep_marks, &trace, manifest).delays_ms);
    }

    Ok(TurnTaking {
        premature_cut_rate: rate(premature_cuts, total_turns),
        endpoint_delay_ms: summarize(&delays_ms),
        missed_endpoint_rate: rate(missed, total_turns),
    })
}

/// Verbatim note appended to every span attributed to the LLM stage when the
/// LLM under measurement is co-located with the harness (see `render_markdown`).
const LLM_MEASURED_LOCAL_NOTE: &str =
    "(measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)";

/// Why one turn's `network_ms` cannot be believed, or `None` when it can (or
/// when there is no RTT to check it against).
///
/// `network_ms` is a residual — `e2e_ms` minus everything the server could
/// account for — so it absorbs every error in the join rather than reporting
/// one. `residual_is_plausible` is the check that catches that; until this
/// was wired in, it was referenced only by its own unit test while the
/// measured RTT went to `--out-marks` and never reached the report.
fn residual_complaint(b: &TurnBreakdown, rtt_p50_ms: Option<f64>) -> Option<String> {
    let rtt = rtt_p50_ms?;
    if residual_is_plausible(b.network_ms, rtt) {
        return None;
    }
    let why = if b.network_ms < 0.0 {
        "negative, so the client/server join for this turn is wrong"
    } else {
        "far above the round trip, so server time went unattributed"
    };
    Some(format!(
        "IMPLAUSIBLE network residual {:.2}ms vs measured p50 RTT {rtt:.2}ms — {why}",
        b.network_ms
    ))
}

/// Renders one turn's spans as a text Gantt chart, bars positioned by
/// `(start_ms, end_ms)` relative to `I` (see `TurnBreakdown::spans`).
/// `label` is the turn's position in this report; the heading also spells out
/// the `(repetition, fixture turn)` pair it came from, since
/// `TurnBreakdown.turn` restarts every repetition and neither it nor the
/// position identifies a turn on its own — the position shifts whenever an
/// earlier turn is unmeasurable and drops out of `Breakdowns::measured`.
///
/// A turn whose residual fails `residual_is_plausible` is called out in its
/// own heading: the stage split below is an aggregate, and a reader scanning
/// waterfalls must not be able to take an untrustworthy turn for a normal one.
fn render_gantt(
    label: &str,
    b: &TurnBreakdown,
    llm_local: bool,
    rtt_p50_ms: Option<f64>,
) -> String {
    const WIDTH: usize = 50;
    let mut out = String::new();
    let complaint = match residual_complaint(b, rtt_p50_ms) {
        Some(c) => format!(" — **{c}**"),
        None => String::new(),
    };
    out.push_str(&format!(
        "### {label} (repetition {}, fixture turn {}, e2e={:.1}ms){complaint}\n\n",
        b.repetition, b.turn, b.e2e_ms
    ));
    if b.spans.is_empty() {
        out.push_str("_(no closed spans recorded for this turn)_\n\n");
        return out;
    }
    let extent = b
        .spans
        .iter()
        .map(|(_, _, end)| *end)
        .fold(b.server_internal_ms.max(1.0), f64::max);

    out.push_str("```\n");
    for (name, start, end) in &b.spans {
        let start_pos = ((start / extent) * WIDTH as f64).round() as usize;
        let start_pos = start_pos.min(WIDTH.saturating_sub(1));
        let end_pos = (((end / extent) * WIDTH as f64).round() as usize)
            .max(start_pos + 1)
            .min(WIDTH);
        let mut bar = vec![' '; WIDTH];
        for c in &mut bar[start_pos..end_pos] {
            *c = '=';
        }
        let bar: String = bar.into_iter().collect();
        let is_llm_row = name.starts_with("llm.");
        let suffix = if llm_local && is_llm_row {
            format!(" {LLM_MEASURED_LOCAL_NOTE}")
        } else {
            String::new()
        };
        out.push_str(&format!(
            "[{bar}] {start:>8.1}-{end:<8.1}ms  {name}{suffix}\n"
        ));
    }
    out.push_str("```\n\n");
    out
}

/// Renders a percentile row for one named stage, stating `n=` up front —
/// every aggregate in this report must state how many samples back it,
/// rather than leaving the reader to assume the same `n` throughout.
fn render_stage_row(name: &str, values: &[f64]) -> String {
    match summarize(values) {
        Some(Summary { n, p50, p90, p99 }) => {
            format!("| {name} | n={n} | {p50:.2} | {p90:.2} | {p99:.2} |\n")
        }
        None => format!(
            "| {name} | n={} | (insufficient samples for a percentile) | | |\n",
            values.len()
        ),
    }
}

/// Like `render_stage_row`, but for a `Summary` already computed by the
/// caller (e.g. `TurnTaking::endpoint_delay_ms`) rather than a raw value
/// slice to re-summarize.
fn render_summary_row(name: &str, summary: &Option<Summary>) -> String {
    match summary {
        Some(Summary { n, p50, p90, p99 }) => {
            format!("| {name} | n={n} | {p50:.2} | {p90:.2} | {p99:.2} |\n")
        }
        None => format!("| {name} | n=0 | (insufficient samples for a percentile) | | |\n"),
    }
}

/// A blockquoted banner naming every repetition whose session did not
/// complete, rendered at the very top of the report. Empty when the run was
/// clean — a banner that appears unconditionally is one a reader learns to
/// skip.
fn render_run_failures(run_failures: &[RunFailure]) -> String {
    if run_failures.is_empty() {
        return String::new();
    }
    let mut out = String::new();
    out.push_str("> ## INCOMPLETE RUN — READ THIS BEFORE ANY NUMBER BELOW\n>\n");
    out.push_str(&format!(
        "> {} of this run's repetition(s) ended before the manifest did:\n>\n",
        run_failures.len()
    ));
    for f in run_failures {
        out.push_str(&format!("> - **repetition {}**: {}\n", f.repetition, f.error));
    }
    out.push_str(
        ">\n> Every turn those repetitions never reached has no `FirstAgentAudio` \
         mark and is therefore counted in `missed_endpoint_rate` below. For those \
         repetitions that number measures a dead session, **not** an endpointing \
         failure, and the latency percentiles are drawn from whatever subset of \
         turns did run.\n\n",
    );
    out
}

/// States how many turns this report's numbers actually cover, and names
/// every turn that could not be measured along with why.
///
/// Rendered before any number and unconditionally: 19-of-20 must never be
/// readable as 20-of-20, and a section that only appears on failure leaves a
/// reader unable to tell "all turns measured" from "coverage not reported".
fn render_coverage(b: &Breakdowns) -> String {
    let mut out = String::new();
    out.push_str("## Turn measurement coverage\n\n");
    if b.unmeasurable.is_empty() {
        out.push_str(&format!(
            "All {} turn(s) attempted in this run were measured; every aggregate below \
             covers all of them.\n\n",
            b.attempted()
        ));
        return out;
    }
    out.push_str(&format!(
        "**{} of {} turn(s) could not be measured and contribute no number anywhere in \
         this report.** Every per-turn aggregate below is over the remaining {}. An \
         unmeasurable turn is excluded, never estimated: a missing number is recoverable, \
         a wrong one is not.\n\n",
        b.unmeasurable.len(),
        b.attempted(),
        b.measured.len()
    ));
    for f in &b.unmeasurable {
        out.push_str(&format!(
            "- **repetition {}, turn {}**: {}\n",
            f.repetition, f.turn, f.reason
        ));
    }
    out.push_str(
        "\nThe usual cause is a premature cut — the server endpointed before the user's \
         marked speech end, so the turn's own `out.first_audio` precedes its `I` and falls \
         outside the one-turn search window. That is a real result, not a harness bug: see \
         `premature_cut_rate` under *Turn-taking quality*, which counts these turns and is \
         the axis the endpointing sweep trades latency against.\n\n",
    );
    out
}

/// The `network` row's plausibility audit: one line per turn whose residual
/// fails `residual_is_plausible` against the run's own measured RTT.
///
/// Always rendered, including when everything passes — "checked, all fine" and
/// "never checked" are different states, and a section that only appears on
/// failure leaves the reader unable to tell which one they are looking at.
fn render_residual_plausibility(breakdowns: &[TurnBreakdown], rtt_p50_ms: Option<f64>) -> String {
    let mut out = String::new();
    out.push_str("### network residual plausibility\n\n");
    let Some(rtt) = rtt_p50_ms else {
        out.push_str(
            "**Not checked**: this run has no RTT probe, so nothing bounds the `network` \
             row above. It is a residual (`e2e_ms - server_internal_ms`), not a \
             measurement — treat it as unvalidated.\n\n",
        );
        return out;
    };
    let complaints: Vec<(usize, &TurnBreakdown, String)> = breakdowns
        .iter()
        .enumerate()
        .filter_map(|(i, b)| residual_complaint(b, Some(rtt)).map(|c| (i, b, c)))
        .collect();
    if complaints.is_empty() {
        out.push_str(&format!(
            "All {} measured turn(s) pass `residual_is_plausible` against the measured \
             p50 RTT of {rtt:.2}ms.\n\n",
            breakdowns.len()
        ));
        return out;
    }
    out.push_str(&format!(
        "**{} of {} measured turn(s) FAIL `residual_is_plausible`.** Their `network_ms` is \
         not a transport cost and their stage split must not be used for any optimization \
         decision; the `network` percentile row above pools them in with the rest.\n\n",
        complaints.len(),
        breakdowns.len()
    ));
    for (i, b, complaint) in complaints {
        // Position alone is not traceable: it shifts when an earlier turn is
        // unmeasurable, and it says nothing about which repetition. A
        // residual complaint is the signal that a sweep point's data cannot
        // be trusted, so it has to name the turn that produced it.
        out.push_str(&format!(
            "- Turn {i} (repetition {}, fixture turn {}): {complaint}\n",
            b.repetition, b.turn
        ));
    }
    out.push('\n');
    out
}

/// Renders the full markdown report, in order: a percentile table (one row
/// per fixture category, each stating `n=`), one ASCII Gantt per turn, and
/// the `detection_lag` / `server_internal` / `network` split.
///
/// `detection_lag_ms` is `Option<f64>` because some turns genuinely have no
/// `endpoint.vad_eot` anchor (see `TurnBreakdown::detection_lag_ms`); `None`
/// values are filtered out before this function feeds anything to
/// `summarize`/`percentile`, whose sort panics on `NaN` and must never be fed
/// a sentinel. Filtering silently would itself mislead, so the report states
/// how many turns were excluded rather than just quietly shrinking `n`.
///
/// When `llm_local` is true, every span whose name starts with `llm.` (the
/// only stage this harness can measure so far) carries a note that the
/// measurement excludes real network cost — this harness's LLM is a
/// self-hosted vLLM on the same cluster, unlike any production deployment
/// with a remote LLM.
///
/// `run_failures` is rendered *before* any number, because a repetition whose
/// session died produces turns with no `FirstAgentAudio` mark, and those are
/// indistinguishable from turns the server genuinely failed to endpoint (see
/// [`RunFailure`]). A reader who does not know the session died will read
/// `missed_endpoint_rate` as an endpointing result.
///
/// `breakdowns` carries both halves of [`breakdown_all`]'s result. The
/// unmeasurable turns get their own section, also before any number, and
/// every aggregate over the measured ones states what it excluded — the same
/// rule already applied to `None` detection lags.
pub fn render_markdown(
    breakdowns: &Breakdowns,
    summaries: &BTreeMap<String, Summary>,
    turn_taking: &TurnTaking,
    llm_local: bool,
    rtt_p50_ms: Option<f64>,
    run_failures: &[RunFailure],
) -> String {
    let mut out = String::new();
    let unmeasurable = breakdowns.unmeasurable.len();
    let attempted = breakdowns.attempted();
    let breakdowns_all = breakdowns;
    let breakdowns: &[TurnBreakdown] = &breakdowns_all.measured;

    out.push_str("# Gradbot latency report\n\n");
    out.push_str(&render_run_failures(run_failures));
    out.push_str(&render_coverage(breakdowns_all));

    out.push_str("## End-to-end latency by fixture category (ms)\n\n");
    out.push_str("| category | n | p50 | p90 | p99 |\n");
    out.push_str("| --- | --- | --- | --- | --- |\n");
    for (category, s) in summaries {
        out.push_str(&format!(
            "| {category} | n={} | {:.2} | {:.2} | {:.2} |\n",
            s.n, s.p50, s.p90, s.p99
        ));
    }
    if summaries.is_empty() {
        out.push_str("_(no category has enough samples to summarize)_\n");
    }
    out.push('\n');

    out.push_str(&format!(
        "## Per-turn waterfalls ({} of {} turn(s) measured)\n\n",
        breakdowns.len(),
        attempted
    ));
    if breakdowns.is_empty() {
        out.push_str("_(no turns to render)_\n\n");
    } else {
        for (i, b) in breakdowns.iter().enumerate() {
            out.push_str(&render_gantt(&format!("Turn {i}"), b, llm_local, rtt_p50_ms));
        }
    }

    out.push_str("## detection_lag / server_internal / network split (ms)\n\n");
    let detection_lag_ms: Vec<f64> = breakdowns.iter().filter_map(|b| b.detection_lag_ms).collect();
    let missing = breakdowns.len() - detection_lag_ms.len();
    out.push_str(&format!(
        "Every row below is over the {} measured turn(s) of {attempted} attempted; the \
         {unmeasurable} unmeasurable turn(s) are in none of them (see *Turn measurement \
         coverage*).\n\n",
        breakdowns.len()
    ));
    out.push_str(&format!(
        "{missing} of those {} measured turn(s) had no detection-lag measurement (no \
         `endpoint.vad_eot` anchor found) and are excluded from the `detection_lag` row \
         below as well — a percentile computed over fewer samples than the reader assumes \
         would itself be misleading.\n\n",
        breakdowns.len()
    ));
    let server_internal_ms: Vec<f64> = breakdowns.iter().map(|b| b.server_internal_ms).collect();
    let network_ms: Vec<f64> = breakdowns.iter().map(|b| b.network_ms).collect();

    if llm_local {
        out.push_str(&format!(
            "LLM stage note: this run's LLM is {LLM_MEASURED_LOCAL_NOTE}\n\n"
        ));
    }

    out.push_str("| stage | n | p50 | p90 | p99 |\n");
    out.push_str("| --- | --- | --- | --- | --- |\n");
    out.push_str(&render_stage_row("detection_lag", &detection_lag_ms));
    out.push_str(&render_stage_row("server_internal", &server_internal_ms));
    out.push_str(&render_stage_row("network", &network_ms));
    out.push('\n');

    out.push_str(&render_residual_plausibility(breakdowns, rtt_p50_ms));

    out.push_str("## Turn-taking quality\n\n");
    out.push_str(
        "These get worse as endpointing is tuned more aggressively — read them \
         alongside the latency numbers above, not instead of them.\n\n",
    );
    out.push_str(&format!(
        "- premature_cut_rate: {:.1}% (agent's first audio arrived before the user \
         actually finished speaking)\n",
        turn_taking.premature_cut_rate * 100.0
    ));
    out.push_str(&format!(
        "- missed_endpoint_rate: {:.1}% (agent never answered the turn at all)\n\n",
        turn_taking.missed_endpoint_rate * 100.0
    ));
    out.push_str("| stage | n | p50 | p90 | p99 |\n");
    out.push_str("| --- | --- | --- | --- | --- |\n");
    out.push_str(&render_summary_row(
        "endpoint_delay_ms",
        &turn_taking.endpoint_delay_ms,
    ));

    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rec(t_us: u64, turn: u64, span: &str, phase: Phase, sample_idx: u64) -> TraceRecord {
        TraceRecord {
            t_us,
            turn,
            span: span.to_string(),
            phase,
            audio_time_s: 0.0,
            sample_idx,
            attrs: serde_json::Map::new(),
        }
    }

    /// A `Breakdowns` with nothing unmeasurable, for tests that are not
    /// exercising per-turn failure isolation themselves.
    fn all_measured(measured: Vec<TurnBreakdown>) -> Breakdowns {
        Breakdowns {
            measured,
            unmeasurable: Vec::new(),
        }
    }

    /// A `TurnTaking` value for tests that render a report but aren't
    /// exercising turn-taking metrics themselves.
    fn no_turn_taking() -> TurnTaking {
        TurnTaking {
            premature_cut_rate: 0.0,
            endpoint_delay_ms: None,
            missed_endpoint_rate: 0.0,
        }
    }

    #[test]
    fn decomposition_needs_no_clock_sync() {
        // Server clock origin is deliberately unrelated to the client's.
        let trace = vec![
            rec(500_000, 0, "audio_in.frame", Phase::Point, 24_000), // I
            rec(560_000, 1, "endpoint.vad_eot", Phase::Point, 25_920), // V
            rec(1_300_000, 1, "out.first_audio", Phase::Point, 0),   // O
        ];
        let marks = vec![
            ClientMark { repetition: 0, turn: 1, kind: MarkKind::UserSpeechEnd, t_us: 9_000_000, sample_idx: 24_000 },
            ClientMark { repetition: 0, turn: 1, kind: MarkKind::FirstAgentAudio, t_us: 9_900_000, sample_idx: 0 },
        ];

        let b = breakdown(&marks, &trace, 1).unwrap();
        assert_eq!(b.e2e_ms, 900.0);
        assert_eq!(b.detection_lag_ms, Some(60.0));   // V - I
        assert_eq!(b.server_internal_ms, 800.0); // O - I
        assert_eq!(b.network_ms, 100.0);         // E2E - (O - I)
    }

    #[test]
    fn join_is_by_sample_index_not_by_time() {
        // The `I` anchor must be the frame carrying the marked sample index,
        // even when a later frame has a closer timestamp.
        let trace = vec![
            rec(100_000, 0, "audio_in.frame", Phase::Point, 24_000),
            rec(900_000, 0, "audio_in.frame", Phase::Point, 48_000),
            rec(950_000, 1, "out.first_audio", Phase::Point, 0),
        ];
        let marks = vec![
            ClientMark { repetition: 0, turn: 1, kind: MarkKind::UserSpeechEnd, t_us: 0, sample_idx: 24_000 },
            ClientMark { repetition: 0, turn: 1, kind: MarkKind::FirstAgentAudio, t_us: 900_000, sample_idx: 0 },
        ];
        let b = breakdown(&marks, &trace, 1).unwrap();
        assert_eq!(b.server_internal_ms, 850.0, "must anchor on sample 24000");
    }

    #[test]
    fn client_and_server_turn_numbering_need_not_agree() {
        // The client counts fixture turns; the server's turn_idx is its own
        // sequence that also advances on interruptions. A join that matched
        // them would mis-attribute every stage after the first interruption.
        // `audio_in.frame` carries turn 0 because that is all its producer can
        // emit — see `TurnBreakdown::spans`.
        let trace = vec![
            rec(100_000, 0, "audio_in.frame", Phase::Point, 24_000),
            rec(160_000, 7, "endpoint.vad_eot", Phase::Point, 25_920),
            rec(900_000, 9, "out.first_audio", Phase::Point, 0),
        ];
        let marks = vec![
            ClientMark { repetition: 0, turn: 1, kind: MarkKind::UserSpeechEnd, t_us: 0, sample_idx: 24_000 },
            ClientMark { repetition: 0, turn: 1, kind: MarkKind::FirstAgentAudio, t_us: 900_000, sample_idx: 0 },
        ];
        // Client turn 1 vs server turns 7 and 9 — none of them equal.
        let b = breakdown(&marks, &trace, 1).unwrap();
        assert_eq!(b.detection_lag_ms, Some(60.0));
        assert_eq!(b.server_internal_ms, 800.0);
    }

    /// The bug this pins: `collect_spans` was keyed on `I`'s turn, and `I` is
    /// an `audio_in.frame`, whose producer hardcodes `turn: 0` because the
    /// input loop has no per-turn context. Every turn's waterfall therefore
    /// rendered *turn 0's* spans, positioned against a much later `I` —
    /// strongly negative offsets that `render_gantt` casts `as usize` and
    /// saturates into 1-char bars. The fixtures that hid it used
    /// `audio_in.frame` records with `turn: 1` / `turn: 7`, values the
    /// producer cannot emit, so `I`'s turn happened to be the right one.
    ///
    /// Here turn 0 (an earlier turn, or the greeting) has its own spans and
    /// the measured turn is server turn 3. The collected spans must be turn
    /// 3's, and their offsets must still be relative to `I`.
    #[test]
    fn spans_come_from_the_turn_that_produced_o_not_from_audio_in_frames_turn_0() {
        let trace = vec![
            // Turn 0's spans — a previous turn, long finished.
            rec(10_000, 0, "llm.push", Phase::Begin, 0),
            rec(20_000, 0, "llm.push", Phase::End, 0),
            // `I` — the producer can only ever stamp this with turn 0.
            rec(500_000, 0, "audio_in.frame", Phase::Point, 24_000),
            // The turn actually being measured, as the server numbered it.
            rec(600_000, 3, "llm.push", Phase::Begin, 24_000),
            rec(650_000, 3, "llm.push", Phase::End, 24_000),
            rec(700_000, 3, "tts.connect", Phase::Begin, 0),
            rec(900_000, 3, "tts.connect", Phase::End, 0),
            rec(1_300_000, 3, "out.first_audio", Phase::Point, 0), // O
        ];
        let marks = vec![
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::UserSpeechEnd, t_us: 9_000_000, sample_idx: 24_000 },
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::FirstAgentAudio, t_us: 9_900_000, sample_idx: 0 },
        ];

        let b = breakdown(&marks, &trace, 0).unwrap();
        assert_eq!(
            b.spans,
            vec![
                // Relative to `I` at 500_000us, as `TurnBreakdown::spans` promises.
                ("llm.push".to_string(), 100.0, 150.0),
                ("tts.connect".to_string(), 200.0, 400.0),
            ],
            "must be server turn 3's spans, not turn 0's, and positioned against I"
        );
        assert!(
            b.spans.iter().all(|(_, start, _)| *start >= 0.0),
            "turn 0's spans precede I and would render as negative offsets: {:?}",
            b.spans
        );
    }

    /// The case that used to produce a `NaN`: no `endpoint.vad_eot` anywhere
    /// in the trace. `detection_lag_ms` must come back `None` — never a
    /// number — while the rest of the breakdown, which does not depend on
    /// `V`, still succeeds and is still correct. This is the whole point of
    /// treating `V` as a soft anchor rather than failing the turn outright.
    #[test]
    fn missing_vad_eot_yields_none_lag_but_the_rest_of_the_breakdown_survives() {
        let trace = vec![
            rec(100_000, 0, "audio_in.frame", Phase::Point, 24_000), // I
            rec(950_000, 1, "out.first_audio", Phase::Point, 0),     // O
        ];
        let marks = vec![
            ClientMark { repetition: 0, turn: 1, kind: MarkKind::UserSpeechEnd, t_us: 9_000_000, sample_idx: 24_000 },
            ClientMark { repetition: 0, turn: 1, kind: MarkKind::FirstAgentAudio, t_us: 9_900_000, sample_idx: 0 },
        ];
        let b = breakdown(&marks, &trace, 1).unwrap();
        assert_eq!(b.detection_lag_ms, None, "no endpoint.vad_eot in the trace: lag is unmeasured, not zero");
        assert_eq!(b.e2e_ms, 900.0);
        assert_eq!(b.server_internal_ms, 850.0, "O must still anchor on I when V is absent");
        assert_eq!(b.network_ms, 50.0);
    }

    /// The failure I3 names: on a **premature cut** the server endpointed
    /// before the client's marked speech end, so this turn's real
    /// `endpoint.vad_eot` and `out.first_audio` both precede `I`. `find_v`
    /// and `find_o` search forward, so unbounded they return the NEXT turn's
    /// records: `detection_lag_ms` and `server_internal_ms` silently become
    /// another turn's numbers and `network_ms` goes negative — printed as a
    /// plain number in the percentile table. Bounded by the next turn's `I`,
    /// nothing is found and the turn fails loudly instead.
    #[test]
    fn a_premature_cut_never_borrows_the_next_turns_anchors() {
        let trace = vec![
            // Turn 0's real answer, produced *before* the client's marked
            // speech end — the server cut the user off.
            rec(100_000, 1, "endpoint.vad_eot", Phase::Point, 20_000),
            rec(200_000, 1, "out.first_audio", Phase::Point, 0),
            rec(500_000, 0, "audio_in.frame", Phase::Point, 24_000), // turn 0's I
            // Turn 1: its own I, and its own (well-behaved) anchors after it.
            rec(3_000_000, 0, "audio_in.frame", Phase::Point, 96_000), // turn 1's I
            rec(3_060_000, 2, "endpoint.vad_eot", Phase::Point, 97_920),
            rec(3_800_000, 2, "out.first_audio", Phase::Point, 0),
        ];
        let marks = vec![
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::UserSpeechEnd, t_us: 9_000_000, sample_idx: 24_000 },
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::FirstAgentAudio, t_us: 8_800_000, sample_idx: 0 },
            ClientMark { repetition: 0, turn: 1, kind: MarkKind::UserSpeechEnd, t_us: 12_000_000, sample_idx: 96_000 },
            ClientMark { repetition: 0, turn: 1, kind: MarkKind::FirstAgentAudio, t_us: 12_900_000, sample_idx: 0 },
        ];

        let err = breakdown(&marks, &trace, 0)
            .expect_err("turn 0's O precedes its I; borrowing turn 1's must not be allowed")
            .to_string();
        assert!(err.contains("out.first_audio"), "got: {err}");
        assert!(err.contains("premature cut"), "the error must name the cause: {err}");

        // Turn 1, whose anchors are where they belong, is unaffected.
        let b = breakdown(&marks, &trace, 1).unwrap();
        assert_eq!(b.detection_lag_ms, Some(60.0));
        assert_eq!(b.server_internal_ms, 800.0);
    }

    /// The ceiling must not swallow a legitimately-measured `V`: a turn whose
    /// detection lands normally, before the next turn begins, still measures.
    #[test]
    fn a_normal_turn_still_finds_its_anchors_inside_the_window() {
        let trace = vec![
            rec(500_000, 0, "audio_in.frame", Phase::Point, 24_000), // turn 0's I
            rec(560_000, 1, "endpoint.vad_eot", Phase::Point, 25_920),
            rec(1_300_000, 1, "out.first_audio", Phase::Point, 0),
            rec(3_000_000, 0, "audio_in.frame", Phase::Point, 96_000), // turn 1's I
            rec(3_060_000, 2, "endpoint.vad_eot", Phase::Point, 97_920),
            rec(3_800_000, 2, "out.first_audio", Phase::Point, 0),
        ];
        let marks = vec![
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::UserSpeechEnd, t_us: 9_000_000, sample_idx: 24_000 },
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::FirstAgentAudio, t_us: 9_900_000, sample_idx: 0 },
            ClientMark { repetition: 0, turn: 1, kind: MarkKind::UserSpeechEnd, t_us: 12_000_000, sample_idx: 96_000 },
            ClientMark { repetition: 0, turn: 1, kind: MarkKind::FirstAgentAudio, t_us: 12_900_000, sample_idx: 0 },
        ];
        let b = breakdown(&marks, &trace, 0).unwrap();
        assert_eq!(b.detection_lag_ms, Some(60.0), "turn 0's own V is inside the window");
        assert_eq!(b.server_internal_ms, 800.0, "turn 0's own O is inside the window");
    }

    /// A `V` that only exists *after* the next turn started is not this
    /// turn's, and "not measured" (`None`) is the only honest answer — the
    /// same one a trace with no `endpoint.vad_eot` at all produces.
    #[test]
    fn a_vad_eot_belonging_to_the_next_turn_reads_as_not_measured() {
        let trace = vec![
            rec(500_000, 0, "audio_in.frame", Phase::Point, 24_000), // turn 0's I
            rec(1_300_000, 1, "out.first_audio", Phase::Point, 0),   // turn 0's O
            rec(3_000_000, 0, "audio_in.frame", Phase::Point, 96_000), // turn 1's I
            rec(3_060_000, 2, "endpoint.vad_eot", Phase::Point, 97_920), // turn 1's V
        ];
        let marks = vec![
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::UserSpeechEnd, t_us: 9_000_000, sample_idx: 24_000 },
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::FirstAgentAudio, t_us: 9_900_000, sample_idx: 0 },
            ClientMark { repetition: 0, turn: 1, kind: MarkKind::UserSpeechEnd, t_us: 12_000_000, sample_idx: 96_000 },
        ];
        let b = breakdown(&marks, &trace, 0).unwrap();
        assert_eq!(
            b.detection_lag_ms, None,
            "turn 1's vad_eot must not be reported as turn 0's detection lag"
        );
        assert_eq!(b.server_internal_ms, 800.0, "O is still turn 0's own");
    }

    /// The last turn has no next turn to bound against, so its search stays
    /// unbounded — a late answer there is still the answer to that turn.
    #[test]
    fn the_last_turn_has_no_ceiling() {
        let trace = vec![
            rec(500_000, 0, "audio_in.frame", Phase::Point, 24_000),
            rec(9_000_000, 1, "out.first_audio", Phase::Point, 0),
        ];
        let marks = vec![
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::UserSpeechEnd, t_us: 1_000_000, sample_idx: 24_000 },
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::FirstAgentAudio, t_us: 9_500_000, sample_idx: 0 },
        ];
        let b = breakdown(&marks, &trace, 0).unwrap();
        assert_eq!(b.server_internal_ms, 8500.0);
    }

    #[test]
    fn missing_anchor_is_an_error_not_a_zero() {
        let trace = vec![rec(100_000, 1, "out.first_audio", Phase::Point, 0)];
        let marks = vec![
            ClientMark { repetition: 0, turn: 1, kind: MarkKind::UserSpeechEnd, t_us: 0, sample_idx: 24_000 },
            ClientMark { repetition: 0, turn: 1, kind: MarkKind::FirstAgentAudio, t_us: 900_000, sample_idx: 0 },
        ];
        assert!(breakdown(&marks, &trace, 1).is_err());
    }

    // --- Task 14: rendering, trace-file pairing, None-filtering ---

    /// Brief's Step 1 test, adapted: the brief's literal snippet builds
    /// `TurnBreakdown { detection_lag_ms: 60.0, .. }`, but that field has been
    /// `Option<f64>` since `27ec2e6` (fix(bench): represent a missing
    /// detection-lag anchor as None, not NaN) — a prior, real fix that
    /// predates this brief and that the brief itself references by name in
    /// its own history. `Some(60.0)` is the only value that still compiles
    /// and preserves the test's intent (a normal, measured turn).
    #[test]
    fn report_labels_the_llm_stage_as_measured_local() {
        let b = TurnBreakdown {
            repetition: 0,
            turn: 1,
            e2e_ms: 900.0,
            detection_lag_ms: Some(60.0),
            server_internal_ms: 800.0,
            network_ms: 100.0,
            spans: vec![("llm.push".to_string(), 10.0, 60.0)],
        };
        let out = render_markdown(&all_measured(vec![b]), &BTreeMap::new(), &no_turn_taking(), true, None, &[]);
        assert!(
            out.contains("measured-local"),
            "a locally-hosted LLM flatters its own stage and must be labelled: {out}"
        );
    }

    /// A locally-hosted LLM must not be mislabelled when the harness is
    /// pointed at a real remote LLM: no row should claim `measured-local`.
    #[test]
    fn report_omits_the_local_label_when_llm_local_is_false() {
        let b = TurnBreakdown {
            repetition: 0,
            turn: 1,
            e2e_ms: 900.0,
            detection_lag_ms: Some(60.0),
            server_internal_ms: 800.0,
            network_ms: 100.0,
            spans: vec![("llm.push".to_string(), 10.0, 60.0)],
        };
        let out = render_markdown(&all_measured(vec![b]), &BTreeMap::new(), &no_turn_taking(), false, None, &[]);
        assert!(!out.contains("measured-local"), "got: {out}");
    }

    #[test]
    fn report_states_the_repetition_count() {
        let mut summaries = BTreeMap::new();
        summaries.insert(
            "short_answer".to_string(),
            Summary { n: 20, p50: 800.0, p90: 950.0, p99: 1100.0 },
        );
        let out = render_markdown(&all_measured(vec![]), &summaries, &no_turn_taking(), false, None, &[]);
        assert!(out.contains("n=20"), "every aggregate must state its n: {out}");
    }

    /// The whole point of Deliverable 3: feeding `render_markdown` a mix of
    /// `Some`/`None` detection-lag turns must not panic (this codebase's
    /// `summarize`/`percentile` panic on `NaN` via `partial_cmp().expect(...)`),
    /// and the omission must be visible in the rendered text, not silently
    /// absorbed into a smaller `n`.
    #[test]
    fn report_filters_none_detection_lag_and_states_the_omission() {
        let with_lag = TurnBreakdown {
            repetition: 0,
            turn: 0,
            e2e_ms: 900.0,
            detection_lag_ms: Some(60.0),
            server_internal_ms: 800.0,
            network_ms: 100.0,
            spans: vec![],
        };
        let without_lag = TurnBreakdown {
            repetition: 0,
            turn: 1,
            e2e_ms: 900.0,
            detection_lag_ms: None,
            server_internal_ms: 800.0,
            network_ms: 100.0,
            spans: vec![],
        };
        let out = render_markdown(&all_measured(vec![with_lag, without_lag]), &BTreeMap::new(), &no_turn_taking(), false, None, &[]);
        assert!(
            out.contains("1 of those 2 measured turn(s) had no detection-lag measurement"),
            "the omission must be stated explicitly, not hidden: {out}"
        );
    }

    #[test]
    fn discover_trace_files_sorts_by_embedded_timestamp_not_filename_order() {
        let dir = std::env::temp_dir().join(format!(
            "gradbot-report-test-{}-{}",
            std::process::id(),
            "sorts_by_embedded_timestamp"
        ));
        std::fs::create_dir_all(&dir).unwrap();
        // Filenames deliberately in the *wrong* lexicographic order relative
        // to their embedded timestamps, so a naive `read_dir` + sort-by-name
        // (or a sort by mtime, which this test does not control) would fail.
        std::fs::write(dir.join("trace_200_000001.jsonl"), "").unwrap();
        std::fs::write(dir.join("trace_100_000000.jsonl"), "").unwrap();
        std::fs::write(dir.join("trace_9999999999_000002.jsonl"), "").unwrap();
        std::fs::write(dir.join("not-a-trace-file.txt"), "").unwrap();

        let files = discover_trace_files(&dir).unwrap();
        let names: Vec<String> = files
            .iter()
            .map(|p| p.file_name().unwrap().to_string_lossy().to_string())
            .collect();
        assert_eq!(
            names,
            vec!["trace_100_000000.jsonl", "trace_200_000001.jsonl", "trace_9999999999_000002.jsonl"],
            "must be ascending by embedded timestamp, non-trace files excluded"
        );

        std::fs::remove_dir_all(&dir).ok();
    }

    /// Deliverable 2's core guarantee: a trace-file/repetition count mismatch
    /// must fail loudly, never truncate or zip-shortest. This is the failure
    /// mode `breakdown` itself structurally cannot detect (see module docs).
    #[test]
    fn breakdown_all_fails_loudly_on_a_trace_file_count_mismatch() {
        let dir = std::env::temp_dir().join(format!(
            "gradbot-report-test-{}-{}",
            std::process::id(),
            "count_mismatch"
        ));
        std::fs::create_dir_all(&dir).unwrap();
        // One trace file...
        std::fs::write(dir.join("trace_100_000000.jsonl"), "").unwrap();

        // ...but marks spanning two repetitions.
        let marks = vec![
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::UserSpeechEnd, t_us: 0, sample_idx: 24_000 },
            ClientMark { repetition: 1, turn: 0, kind: MarkKind::UserSpeechEnd, t_us: 0, sample_idx: 24_000 },
        ];

        let err = breakdown_all(&marks, &dir).unwrap_err().to_string();
        assert!(err.contains("mismatch"), "got: {err}");
        assert!(err.contains('1'), "should name the trace-file count: {err}");
        assert!(err.contains('2'), "should name the repetition count: {err}");

        std::fs::remove_dir_all(&dir).ok();
    }

    /// The happy path: N trace files, N repetitions, correct pairing by
    /// timestamp order — and the resulting breakdowns are correct, not just
    /// "no error was raised".
    #[test]
    fn breakdown_all_pairs_each_repetition_with_its_own_trace_file_in_timestamp_order() {
        let dir = std::env::temp_dir().join(format!(
            "gradbot-report-test-{}-{}",
            std::process::id(),
            "happy_path"
        ));
        std::fs::create_dir_all(&dir).unwrap();

        let rec_line = |t_us: u64, turn: u64, span: &str, sample_idx: u64| {
            serde_json::to_string(&rec(t_us, turn, span, Phase::Point, sample_idx)).unwrap()
        };

        // Repetition 0's trace (written first, smaller embedded timestamp):
        // server_internal_ms = 800.0.
        std::fs::write(
            dir.join("trace_100_000000.jsonl"),
            format!(
                "{}\n{}\n",
                rec_line(500_000, 0, "audio_in.frame", 24_000),
                rec_line(1_300_000, 1, "out.first_audio", 0),
            ),
        )
        .unwrap();
        // Repetition 1's trace (written second): server_internal_ms = 400.0.
        // If the pairing were reversed (or zipped in file-listing order on a
        // filesystem that doesn't guarantee name order), this turn would pick
        // up the wrong numbers with no error.
        std::fs::write(
            dir.join("trace_200_000001.jsonl"),
            format!(
                "{}\n{}\n",
                rec_line(500_000, 0, "audio_in.frame", 24_000),
                rec_line(900_000, 1, "out.first_audio", 0),
            ),
        )
        .unwrap();

        let marks = vec![
            ClientMark { repetition: 0, turn: 1, kind: MarkKind::UserSpeechEnd, t_us: 9_000_000, sample_idx: 24_000 },
            ClientMark { repetition: 0, turn: 1, kind: MarkKind::FirstAgentAudio, t_us: 9_900_000, sample_idx: 0 },
            ClientMark { repetition: 1, turn: 1, kind: MarkKind::UserSpeechEnd, t_us: 9_000_000, sample_idx: 24_000 },
            ClientMark { repetition: 1, turn: 1, kind: MarkKind::FirstAgentAudio, t_us: 9_900_000, sample_idx: 0 },
        ];

        let mut breakdowns = breakdown_all(&marks, &dir).unwrap();
        assert!(breakdowns.unmeasurable.is_empty(), "every turn here measures cleanly");
        let measured = &mut breakdowns.measured;
        measured.sort_by(|a, b| a.server_internal_ms.partial_cmp(&b.server_internal_ms).unwrap());
        assert_eq!(measured.len(), 2);
        assert_eq!(measured[0].server_internal_ms, 400.0, "repetition 1 must pair with its own (later) trace file");
        assert_eq!(measured[1].server_internal_ms, 800.0, "repetition 0 must pair with its own (earlier) trace file");
        // Each breakdown must know which repetition it came from, or a report
        // bullet naming it cannot be traced back to one turn.
        assert_eq!(measured[0].repetition, 1);
        assert_eq!(measured[1].repetition, 0);

        std::fs::remove_dir_all(&dir).ok();
    }

    /// The failure this follow-up exists to prevent: one turn that cannot be
    /// decomposed used to abort the entire report via `?`. The endpointing
    /// sweep drives `flush_duration_s` / `min_listen_before_flush_s` down
    /// *until* premature cuts appear, so every sweep point aggressive enough
    /// to be interesting produced no output at all — and the Pareto operating
    /// point sits exactly there. One cut turn must cost that turn, not the
    /// other nineteen.
    #[test]
    fn one_unmeasurable_turn_does_not_abort_the_other_turns() {
        let dir = std::env::temp_dir().join(format!(
            "gradbot-report-test-{}-{}",
            std::process::id(),
            "one_unmeasurable_turn"
        ));
        std::fs::create_dir_all(&dir).unwrap();

        let rec_line = |t_us: u64, turn: u64, span: &str, sample_idx: u64| {
            serde_json::to_string(&rec(t_us, turn, span, Phase::Point, sample_idx)).unwrap()
        };

        // Turn 0 measures cleanly (server_internal 800ms). Turn 1 was cut
        // prematurely: its own `out.first_audio` precedes its `I`, so nothing
        // lands in its window. Turn 2 measures cleanly again (400ms).
        std::fs::write(
            dir.join("trace_100_000000.jsonl"),
            [
                rec_line(500_000, 0, "audio_in.frame", 24_000), // turn 0's I
                rec_line(1_300_000, 1, "out.first_audio", 0),   // turn 0's O
                rec_line(2_000_000, 2, "out.first_audio", 0),   // turn 1's O — before turn 1's I
                rec_line(3_000_000, 0, "audio_in.frame", 96_000), // turn 1's I
                rec_line(6_000_000, 0, "audio_in.frame", 192_000), // turn 2's I
                rec_line(6_400_000, 3, "out.first_audio", 0),   // turn 2's O
            ]
            .join("\n")
                + "\n",
        )
        .unwrap();

        let marks = vec![
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::UserSpeechEnd, t_us: 1_000_000, sample_idx: 24_000 },
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::FirstAgentAudio, t_us: 1_900_000, sample_idx: 0 },
            ClientMark { repetition: 0, turn: 1, kind: MarkKind::UserSpeechEnd, t_us: 4_000_000, sample_idx: 96_000 },
            ClientMark { repetition: 0, turn: 1, kind: MarkKind::FirstAgentAudio, t_us: 3_800_000, sample_idx: 0 },
            ClientMark { repetition: 0, turn: 2, kind: MarkKind::UserSpeechEnd, t_us: 7_000_000, sample_idx: 192_000 },
            ClientMark { repetition: 0, turn: 2, kind: MarkKind::FirstAgentAudio, t_us: 7_500_000, sample_idx: 0 },
        ];

        let out = breakdown_all(&marks, &dir).expect("one cut turn must not abort the run");

        // The successes are all present, and correct — not merely "no error".
        assert_eq!(out.measured.len(), 2, "turns 0 and 2 measured: {:?}", out.measured);
        assert_eq!(out.measured[0].turn, 0);
        assert_eq!(out.measured[0].server_internal_ms, 800.0);
        assert_eq!(out.measured[1].turn, 2);
        assert_eq!(out.measured[1].server_internal_ms, 400.0);
        assert_eq!(out.attempted(), 3);

        // The failure is recorded, identified, and explained.
        assert_eq!(out.unmeasurable.len(), 1, "got: {:?}", out.unmeasurable);
        let f = &out.unmeasurable[0];
        assert_eq!((f.repetition, f.turn), (0, 1));
        assert!(f.reason.contains("out.first_audio"), "must name the anchor: {}", f.reason);
        assert!(f.reason.contains("premature cut"), "must explain why: {}", f.reason);

        // And no number for the unmeasurable turn leaked in anywhere.
        assert!(
            out.measured.iter().all(|b| b.turn != 1),
            "an unmeasurable turn must contribute no number at all: {:?}",
            out.measured
        );

        std::fs::remove_dir_all(&dir).ok();
    }

    /// 19-of-20 must never be readable as 20-of-20: the rendered report has to
    /// state both counts, name the excluded turn and its reason, and say so
    /// again wherever an aggregate is drawn over the shrunken sample.
    #[test]
    fn the_report_states_both_the_measured_and_the_excluded_counts() {
        let measured = TurnBreakdown {
            repetition: 0,
            turn: 0,
            e2e_ms: 900.0,
            detection_lag_ms: Some(60.0),
            server_internal_ms: 800.0,
            network_ms: 100.0,
            spans: vec![],
        };
        let breakdowns = Breakdowns {
            measured: vec![measured],
            unmeasurable: vec![TurnFailure {
                repetition: 0,
                turn: 1,
                reason: "missing span: out.first_audio (O) — the usual cause is a premature cut"
                    .to_string(),
            }],
        };
        let out = render_markdown(
            &breakdowns,
            &BTreeMap::new(),
            &no_turn_taking(),
            false,
            None,
            &[],
        );

        assert!(
            out.contains("1 of 2 turn(s) could not be measured"),
            "both counts must be stated up front: {out}"
        );
        assert!(
            out.contains("**repetition 0, turn 1**"),
            "the excluded turn must be identified: {out}"
        );
        assert!(
            out.contains("premature cut"),
            "the reason must be shown, not just the count: {out}"
        );
        assert!(
            out.contains("Per-turn waterfalls (1 of 2 turn(s) measured)"),
            "the waterfall section must state its coverage: {out}"
        );
        assert!(
            out.contains("over the 1 measured turn(s) of 2 attempted"),
            "the stage table must state what it excluded: {out}"
        );
        // The coverage statement must precede every number it qualifies.
        let coverage = out.find("could not be measured").unwrap();
        assert!(coverage < out.find("Per-turn waterfalls").unwrap(), "{out}");
    }

    /// "All turns measured" and "coverage not reported" must not look alike —
    /// the same rule the residual audit follows.
    #[test]
    fn a_fully_measured_run_still_states_its_coverage() {
        let b = TurnBreakdown {
            repetition: 0,
            turn: 0,
            e2e_ms: 900.0,
            detection_lag_ms: Some(60.0),
            server_internal_ms: 800.0,
            network_ms: 100.0,
            spans: vec![],
        };
        let out = render_markdown(&all_measured(vec![b]), &BTreeMap::new(), &no_turn_taking(), false, None, &[]);
        assert!(
            out.contains("All 1 turn(s) attempted in this run were measured"),
            "got: {out}"
        );
        assert!(!out.contains("could not be measured"), "got: {out}");
    }

    // --- Task 19: turn-taking quality metrics ---

    fn manifest_with_turns(n: usize) -> Manifest {
        Manifest {
            name: "test".to_string(),
            turns: (0..n)
                .map(|i| crate::fixtures::FixtureTurn {
                    wav: PathBuf::from(format!("turn{i}.wav")),
                    speech_start_sample: 0,
                    speech_end_sample: 24_000,
                    gap_after_s: 3.0,
                    category: "short_answer".to_string(),
                })
                .collect(),
        }
    }

    /// Brief's Step 1 test, adapted: `ClientMark` requires `repetition` (it
    /// has no `Default`), and `is_premature_cut` keys on `(repetition,
    /// turn)`, not `turn` alone (see its doc comment), so it takes
    /// `repetition` as an explicit third argument rather than the brief's
    /// literal 2-arg call.
    #[test]
    fn agent_audio_before_ground_truth_speech_end_is_a_premature_cut() {
        // The agent answered 100ms before the user had finished speaking.
        let marks = vec![
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::UserSpeechEnd, t_us: 1_000_000, sample_idx: 24_000 },
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::FirstAgentAudio, t_us: 900_000, sample_idx: 0 },
        ];
        assert!(is_premature_cut(&marks, 0, 0));
    }

    #[test]
    fn answering_after_speech_end_is_not_a_cut() {
        let marks = vec![
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::UserSpeechEnd, t_us: 1_000_000, sample_idx: 24_000 },
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::FirstAgentAudio, t_us: 1_800_000, sample_idx: 0 },
        ];
        assert!(!is_premature_cut(&marks, 0, 0));
    }

    #[test]
    fn a_turn_with_no_agent_audio_is_a_missed_endpoint_not_a_cut() {
        let marks = vec![ClientMark {
            repetition: 0, turn: 0, kind: MarkKind::UserSpeechEnd, t_us: 1_000_000, sample_idx: 24_000,
        }];
        assert!(!is_premature_cut(&marks, 0, 0));
    }

    /// The exact bug this codebase already caught once (see `e2e_ms`):
    /// `turn` resets every repetition, so a premature cut in repetition 0's
    /// turn 0 must not bleed into repetition 1's turn 0, which answered
    /// late.
    #[test]
    fn is_premature_cut_keys_on_repetition_and_turn_not_turn_alone() {
        let marks = vec![
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::UserSpeechEnd, t_us: 1_000_000, sample_idx: 24_000 },
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::FirstAgentAudio, t_us: 900_000, sample_idx: 0 },
            ClientMark { repetition: 1, turn: 0, kind: MarkKind::UserSpeechEnd, t_us: 1_000_000, sample_idx: 24_000 },
            ClientMark { repetition: 1, turn: 0, kind: MarkKind::FirstAgentAudio, t_us: 1_800_000, sample_idx: 0 },
        ];
        assert!(is_premature_cut(&marks, 0, 0), "repetition 0 was a premature cut");
        assert!(!is_premature_cut(&marks, 1, 0), "repetition 1 answered on time");
    }

    /// A turn with no marks at all (not even `UserSpeechEnd` — e.g. the
    /// session dropped before the turn played) must still count toward
    /// `missed_endpoint_rate`'s denominator via `manifest`, not just be
    /// invisible because no mark ever named it.
    #[test]
    fn missed_endpoint_rate_counts_turns_absent_from_marks_entirely() {
        let manifest = manifest_with_turns(2);
        // Only turn 0 has any marks; turn 1 never appears anywhere.
        let marks = vec![
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::UserSpeechEnd, t_us: 1_000_000, sample_idx: 24_000 },
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::FirstAgentAudio, t_us: 1_800_000, sample_idx: 0 },
        ];
        let tt = turn_taking(&marks, &[], &manifest);
        assert_eq!(tt.missed_endpoint_rate, 0.5, "1 of 2 manifest turns has no FirstAgentAudio mark");
        assert_eq!(tt.premature_cut_rate, 0.0);
    }

    /// `endpoint_delay_ms` (`V - I`, anchored on the `UserSpeechEnd` mark's
    /// sample index) does not require agent audio to exist at all — unlike
    /// `breakdown`, which fails the whole turn without an `O` anchor. A
    /// turn that missed its endpoint entirely can still tell us how late
    /// detection was.
    #[test]
    fn endpoint_delay_ms_is_measured_even_when_the_endpoint_was_missed() {
        // Two turns, neither ever answered, so `summarize` (which needs
        // n>=2) has enough delay samples to produce a `Summary`.
        let manifest = manifest_with_turns(2);
        let trace = vec![
            rec(500_000, 0, "audio_in.frame", Phase::Point, 24_000), // turn 0's I
            rec(560_000, 1, "endpoint.vad_eot", Phase::Point, 25_920), // turn 0's V (delay 60ms)
            rec(1_000_000, 0, "audio_in.frame", Phase::Point, 48_000), // turn 1's I
            rec(1_100_000, 2, "endpoint.vad_eot", Phase::Point, 49_920), // turn 1's V (delay 100ms)
        ];
        let marks = vec![
            ClientMark { repetition: 0, turn: 0, kind: MarkKind::UserSpeechEnd, t_us: 9_000_000, sample_idx: 24_000 },
            ClientMark { repetition: 0, turn: 1, kind: MarkKind::UserSpeechEnd, t_us: 19_000_000, sample_idx: 48_000 },
        ];
        let tt = turn_taking(&marks, &trace, &manifest);
        assert_eq!(tt.missed_endpoint_rate, 1.0, "neither turn ever got a FirstAgentAudio mark");
        let delay = tt.endpoint_delay_ms.expect("n=2 delay samples is enough for a Summary");
        assert_eq!(delay.n, 2);
    }

    /// A repetition whose session died produces turns with no
    /// `FirstAgentAudio` mark, which `missed_endpoint_rate` counts exactly
    /// like a turn the server failed to endpoint. The report must name the
    /// dead session before the reader reaches that number, or the run gets
    /// filed as an endpointing regression.
    #[test]
    fn a_failed_session_is_reported_as_an_incomplete_run_not_as_missed_endpoints() {
        let tt = TurnTaking {
            premature_cut_rate: 0.0,
            endpoint_delay_ms: None,
            missed_endpoint_rate: 1.0,
        };
        let failures = vec![RunFailure {
            repetition: 2,
            error: "recv_loop error: Error from server: timeout".to_string(),
        }];
        let out = render_markdown(&all_measured(vec![]), &BTreeMap::new(), &tt, false, None, &failures);

        assert!(out.contains("INCOMPLETE RUN"), "the banner must be present: {out}");
        assert!(out.contains("repetition 2"), "the failed repetition must be named: {out}");
        assert!(
            out.contains("Error from server: timeout"),
            "the underlying error must be shown, not just its existence: {out}"
        );
        let banner = out.find("INCOMPLETE RUN").unwrap();
        let missed = out.find("missed_endpoint_rate").unwrap();
        assert!(
            banner < missed,
            "the banner must come before the metric it disclaims: {out}"
        );
    }

    /// `residual_is_plausible` documented itself as the check that catches a
    /// broken join, and was referenced only by its own unit test: the measured
    /// RTT went to `--out-marks` and never reached the report, so a turn whose
    /// `network_ms` was negative rendered as an ordinary row. It must now be
    /// impossible to read such a turn as normal.
    #[test]
    fn an_implausible_residual_is_called_out_per_turn() {
        // Deliberately not at position 0 of its own repetition, and not
        // repetition 0: the report's positional index is *not* the turn's
        // identity, and a complaint that carries only the position cannot be
        // traced back to the turn that produced it.
        let bad = TurnBreakdown {
            repetition: 3,
            turn: 4,
            e2e_ms: 700.0,
            detection_lag_ms: Some(60.0),
            server_internal_ms: 900.0,
            network_ms: -200.0, // e2e < server_internal: the join is wrong
            spans: vec![("llm.push".to_string(), 10.0, 60.0)],
        };
        let good = TurnBreakdown {
            repetition: 3,
            turn: 5,
            e2e_ms: 900.0,
            detection_lag_ms: Some(60.0),
            server_internal_ms: 880.0,
            network_ms: 20.0,
            spans: vec![("llm.push".to_string(), 10.0, 60.0)],
        };
        let out = render_markdown(
            &all_measured(vec![bad, good]),
            &BTreeMap::new(),
            &no_turn_taking(),
            false,
            Some(20.0),
            &[],
        );

        assert!(
            out.contains("1 of 2 measured turn(s) FAIL"),
            "the audit must count the failures: {out}"
        );
        assert!(
            out.contains("- Turn 0 (repetition 3, fixture turn 4): IMPLAUSIBLE"),
            "the audit bullet must identify the turn, not just its position: {out}"
        );
        assert!(
            !out.contains("Turn 1 (repetition 3, fixture turn 5): IMPLAUSIBLE"),
            "the plausible turn must not be flagged: {out}"
        );
        // And the waterfall itself, not just the audit section.
        let heading = out
            .lines()
            .find(|l| l.starts_with("### Turn 0 "))
            .expect("turn 0 must have a waterfall heading");
        assert!(
            heading.contains("IMPLAUSIBLE"),
            "a reader scanning waterfalls must not take it for a normal turn: {heading}"
        );
        assert!(
            heading.contains("repetition 3, fixture turn 4"),
            "the waterfall heading must identify the turn too: {heading}"
        );
    }

    /// "Checked, all fine" and "never checked" must not look the same.
    #[test]
    fn the_residual_audit_distinguishes_all_clear_from_never_checked() {
        let b = TurnBreakdown {
            repetition: 0,
            turn: 0,
            e2e_ms: 900.0,
            detection_lag_ms: Some(60.0),
            server_internal_ms: 880.0,
            network_ms: 20.0,
            spans: vec![],
        };
        let checked = render_markdown(
            &all_measured(vec![b.clone()]),
            &BTreeMap::new(),
            &no_turn_taking(),
            false,
            Some(20.0),
            &[],
        );
        assert!(checked.contains("All 1 measured turn(s) pass"), "got: {checked}");

        let unchecked =
            render_markdown(&all_measured(vec![b]), &BTreeMap::new(), &no_turn_taking(), false, None, &[]);
        assert!(unchecked.contains("Not checked"), "got: {unchecked}");
        assert!(
            !unchecked.contains("pass `residual_is_plausible`"),
            "no RTT means no verdict at all: {unchecked}"
        );
    }

    /// The INCOMPLETE RUN banner must not appear on a clean run: one that
    /// shows up every time is one the reader stops seeing.
    #[test]
    fn a_clean_run_renders_no_incomplete_run_banner() {
        let out = render_markdown(&all_measured(vec![]), &BTreeMap::new(), &no_turn_taking(), false, None, &[]);
        assert!(!out.contains("INCOMPLETE RUN"), "got: {out}");
    }

    #[test]
    fn render_markdown_surfaces_all_three_turn_taking_metrics() {
        let tt = TurnTaking {
            premature_cut_rate: 0.25,
            endpoint_delay_ms: Some(Summary { n: 4, p50: 60.0, p90: 90.0, p99: 99.0 }),
            missed_endpoint_rate: 0.1,
        };
        let out = render_markdown(&all_measured(vec![]), &BTreeMap::new(), &tt, false, None, &[]);
        assert!(out.contains("25.0%"), "premature_cut_rate must be surfaced: {out}");
        assert!(out.contains("10.0%"), "missed_endpoint_rate must be surfaced: {out}");
        assert!(out.contains("endpoint_delay_ms"), "endpoint_delay_ms must be surfaced: {out}");
        assert!(out.contains("60.00"), "endpoint_delay_ms's p50 must be surfaced: {out}");
    }
}
