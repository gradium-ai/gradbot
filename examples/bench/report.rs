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
use super::{ClientMark, MarkKind, Summary, summarize};
use anyhow::{Context, Result};
use gradbot::{Phase, TraceRecord};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

/// One turn's latency, decomposed into stages, plus the spans that make up
/// `server_internal_ms` for a Gantt-style render.
#[derive(Debug, Clone, PartialEq)]
pub struct TurnBreakdown {
    pub turn: u64,
    /// First agent audio minus user speech end, on the client's own clock.
    pub e2e_ms: f64,
    /// `V - I`: how long the server took to detect end-of-turn after the
    /// sample the client marked as speech end. `None` when no
    /// `endpoint.vad_eot` was found for this turn — some traces genuinely
    /// carry no such event (`find_v` is a soft anchor). This is `Option`,
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
    /// `I`. Spans left open (e.g. an abandoned `endpoint.flush`) are omitted
    /// here — they have no end to draw — but never cause `breakdown` itself
    /// to fail; only a missing `I`, `V`, or `O` anchor does that.
    pub spans: Vec<(String, f64, f64)>,
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

/// `V`: the first `endpoint.vad_eot` at or after `t_us_floor` — by time, not
/// by turn. Unlike `I` and `O`, a missing `V` does not fail the whole
/// breakdown: some traces genuinely carry no endpoint-detection event for a
/// turn (see `breakdown`'s handling of `None` here), and `server_internal_ms`
/// — the number this profiler exists to produce — does not depend on it.
fn find_v(trace: &[TraceRecord], t_us_floor: u64) -> Option<&TraceRecord> {
    trace
        .iter()
        .filter(|r| r.span == "endpoint.vad_eot" && r.t_us >= t_us_floor)
        .min_by_key(|r| r.t_us)
}

/// `O`: the first `out.first_audio` at or after `t_us_floor` — by time, not
/// by turn.
fn find_o(trace: &[TraceRecord], t_us_floor: u64) -> Result<&TraceRecord> {
    trace
        .iter()
        .filter(|r| r.span == "out.first_audio" && r.t_us >= t_us_floor)
        .min_by_key(|r| r.t_us)
        .context("missing span: out.first_audio (O)")
}

/// Every closed Begin/End pair recorded under `server_turn`, as
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
    let v = find_v(trace, i.t_us);
    // `O` follows `V` when `V` was found (the normal case: this also guards
    // against picking up a stale `out.first_audio` from before the endpoint
    // was actually detected). When no `endpoint.vad_eot` exists for this
    // turn, fall back to searching from `I` — `O` must not become
    // unmeasurable just because the detection-lag diagnostic is unavailable.
    let o = find_o(trace, v.map_or(i.t_us, |v| v.t_us))?;

    // A missing `V` is "not measured", not "zero lag" (see `find_v` and the
    // field doc on `TurnBreakdown::detection_lag_ms`): `None` makes that
    // explicit rather than smuggling it through as a number.
    let detection_lag_ms = v.map(|v| (v.t_us as f64 - i.t_us as f64) / 1000.0);
    let server_internal_ms = (o.t_us as f64 - i.t_us as f64) / 1000.0;
    let network_ms = e2e_ms - server_internal_ms;

    let spans = collect_spans(trace, i.turn, i.t_us);

    Ok(TurnBreakdown {
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
pub fn breakdown_all(marks: &[ClientMark], trace_dir: &Path) -> Result<Vec<TurnBreakdown>> {
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

    let mut out = Vec::new();
    for (repetition, trace_path) in repetitions.into_iter().zip(trace_files.iter()) {
        let trace = load_trace_file(trace_path)?;
        let rep_marks: Vec<ClientMark> = marks
            .iter()
            .filter(|m| m.repetition == repetition)
            .cloned()
            .collect();
        let turns: BTreeSet<u64> = rep_marks.iter().map(|m| m.turn).collect();
        for turn in turns {
            out.push(breakdown(&rep_marks, &trace, turn).with_context(|| {
                format!("repetition {repetition} (trace file {}), turn {turn}", trace_path.display())
            })?);
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
            && let Some(v) = find_v(trace, i.t_us)
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

/// Renders one turn's spans as a text Gantt chart, bars positioned by
/// `(start_ms, end_ms)` relative to `I` (see `TurnBreakdown::spans`).
/// `label` identifies the turn in the heading — since `TurnBreakdown.turn` is
/// the *client's* per-repetition turn counter (see module docs), it repeats
/// across repetitions and cannot uniquely label a turn across a whole
/// multi-repetition run on its own.
fn render_gantt(label: &str, b: &TurnBreakdown, llm_local: bool) -> String {
    const WIDTH: usize = 50;
    let mut out = String::new();
    out.push_str(&format!(
        "### {label} (fixture turn {}, e2e={:.1}ms)\n\n",
        b.turn, b.e2e_ms
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
pub fn render_markdown(
    breakdowns: &[TurnBreakdown],
    summaries: &BTreeMap<String, Summary>,
    turn_taking: &TurnTaking,
    llm_local: bool,
) -> String {
    let mut out = String::new();

    out.push_str("# Gradbot latency report\n\n");

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

    out.push_str("## Per-turn waterfalls\n\n");
    if breakdowns.is_empty() {
        out.push_str("_(no turns to render)_\n\n");
    } else {
        for (i, b) in breakdowns.iter().enumerate() {
            out.push_str(&render_gantt(&format!("Turn {i}"), b, llm_local));
        }
    }

    out.push_str("## detection_lag / server_internal / network split (ms)\n\n");
    let detection_lag_ms: Vec<f64> = breakdowns.iter().filter_map(|b| b.detection_lag_ms).collect();
    let missing = breakdowns.len() - detection_lag_ms.len();
    out.push_str(&format!(
        "{missing} of {} turn(s) had no detection-lag measurement (no `endpoint.vad_eot` \
         anchor found) and are excluded from the `detection_lag` row below — a percentile \
         computed over fewer samples than the reader assumes would itself be misleading.\n\n",
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
            rec(500_000, 1, "audio_in.frame", Phase::Point, 24_000), // I
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
            rec(100_000, 1, "audio_in.frame", Phase::Point, 24_000),
            rec(900_000, 1, "audio_in.frame", Phase::Point, 48_000),
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
        let trace = vec![
            rec(100_000, 7, "audio_in.frame", Phase::Point, 24_000),
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

    /// The case that used to produce a `NaN`: no `endpoint.vad_eot` anywhere
    /// in the trace. `detection_lag_ms` must come back `None` — never a
    /// number — while the rest of the breakdown, which does not depend on
    /// `V`, still succeeds and is still correct. This is the whole point of
    /// treating `V` as a soft anchor rather than failing the turn outright.
    #[test]
    fn missing_vad_eot_yields_none_lag_but_the_rest_of_the_breakdown_survives() {
        let trace = vec![
            rec(100_000, 1, "audio_in.frame", Phase::Point, 24_000), // I
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
            turn: 1,
            e2e_ms: 900.0,
            detection_lag_ms: Some(60.0),
            server_internal_ms: 800.0,
            network_ms: 100.0,
            spans: vec![("llm.push".to_string(), 10.0, 60.0)],
        };
        let out = render_markdown(&[b], &BTreeMap::new(), &no_turn_taking(), true);
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
            turn: 1,
            e2e_ms: 900.0,
            detection_lag_ms: Some(60.0),
            server_internal_ms: 800.0,
            network_ms: 100.0,
            spans: vec![("llm.push".to_string(), 10.0, 60.0)],
        };
        let out = render_markdown(&[b], &BTreeMap::new(), &no_turn_taking(), false);
        assert!(!out.contains("measured-local"), "got: {out}");
    }

    #[test]
    fn report_states_the_repetition_count() {
        let mut summaries = BTreeMap::new();
        summaries.insert(
            "short_answer".to_string(),
            Summary { n: 20, p50: 800.0, p90: 950.0, p99: 1100.0 },
        );
        let out = render_markdown(&[], &summaries, &no_turn_taking(), false);
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
            turn: 0,
            e2e_ms: 900.0,
            detection_lag_ms: Some(60.0),
            server_internal_ms: 800.0,
            network_ms: 100.0,
            spans: vec![],
        };
        let without_lag = TurnBreakdown {
            turn: 1,
            e2e_ms: 900.0,
            detection_lag_ms: None,
            server_internal_ms: 800.0,
            network_ms: 100.0,
            spans: vec![],
        };
        let out = render_markdown(&[with_lag, without_lag], &BTreeMap::new(), &no_turn_taking(), false);
        assert!(
            out.contains("1 of 2 turn(s) had no detection-lag measurement"),
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
                rec_line(500_000, 1, "audio_in.frame", 24_000),
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
                rec_line(500_000, 1, "audio_in.frame", 24_000),
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
        breakdowns.sort_by(|a, b| a.server_internal_ms.partial_cmp(&b.server_internal_ms).unwrap());
        assert_eq!(breakdowns.len(), 2);
        assert_eq!(breakdowns[0].server_internal_ms, 400.0, "repetition 1 must pair with its own (later) trace file");
        assert_eq!(breakdowns[1].server_internal_ms, 800.0, "repetition 0 must pair with its own (earlier) trace file");

        std::fs::remove_dir_all(&dir).ok();
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
            rec(500_000, 1, "audio_in.frame", Phase::Point, 24_000), // turn 0's I
            rec(560_000, 1, "endpoint.vad_eot", Phase::Point, 25_920), // turn 0's V (delay 60ms)
            rec(1_000_000, 2, "audio_in.frame", Phase::Point, 48_000), // turn 1's I
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

    #[test]
    fn render_markdown_surfaces_all_three_turn_taking_metrics() {
        let tt = TurnTaking {
            premature_cut_rate: 0.25,
            endpoint_delay_ms: Some(Summary { n: 4, p50: 60.0, p90: 90.0, p99: 99.0 }),
            missed_endpoint_rate: 0.1,
        };
        let out = render_markdown(&[], &BTreeMap::new(), &tt, false);
        assert!(out.contains("25.0%"), "premature_cut_rate must be surfaced: {out}");
        assert!(out.contains("10.0%"), "missed_endpoint_rate must be surfaced: {out}");
        assert!(out.contains("endpoint_delay_ms"), "endpoint_delay_ms must be surfaced: {out}");
        assert!(out.contains("60.00"), "endpoint_delay_ms's p50 must be surfaced: {out}");
    }
}
