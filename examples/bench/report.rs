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
//! Repetition scoping happens one level up, before this module is called:
//! `--repetitions N` opens N fresh sessions, so both `ClientMark.turn` and
//! `TraceRecord.sample_idx` restart at zero every repetition (a new session
//! restarts the server's sample counter too). `marks` and `trace` here must
//! already be scoped to a single repetition — pair repetition *i*'s marks
//! with the *i*-th trace file in timestamp order (valid because the harness
//! runs exactly one session at a time) before calling `breakdown`. Joining
//! across repetitions would pair the wrong marks with the wrong trace and
//! produce confident, wrong numbers with no error anywhere.

use super::{ClientMark, MarkKind};
use anyhow::{Context, Result};
use gradbot::{Phase, TraceRecord};

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
}
