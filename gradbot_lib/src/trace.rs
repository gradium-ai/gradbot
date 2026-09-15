//! Wall-clock span tracing for latency profiling.
//!
//! One monotonic `Instant` per session; every stamp is microseconds since that
//! origin. Causal ordering therefore holds by construction. Records go to a
//! side channel (a JSONL file), never to the client WebSocket — shipping them
//! in-band would add traffic to the exact path being measured.

/// Which edge of a span a record marks.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Phase {
    Begin,
    End,
    /// An instantaneous event with no duration (e.g. first token observed).
    Point,
}

/// One span boundary, written as a single JSONL line.
///
/// `t_us` is microseconds since the session clock origin — never a wall-clock
/// date, and never derived by subtracting across two clocks.
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct TraceRecord {
    pub t_us: u64,
    pub turn: u64,
    pub span: String,
    pub phase: Phase,
    /// The existing sample-count clock, recorded alongside wall time so the
    /// drift documented at `multiplex.rs:947` becomes a measured quantity.
    pub audio_time_s: f64,
    /// Cumulative input sample index — the join key against the benchmark
    /// client, which counts the same samples.
    pub sample_idx: u64,
    #[serde(default, skip_serializing_if = "serde_json::Map::is_empty")]
    pub attrs: serde_json::Map<String, serde_json::Value>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn trace_record_jsonl_round_trip() {
        let rec = TraceRecord {
            t_us: 1_234_567,
            turn: 3,
            span: "tts.connect".to_string(),
            phase: Phase::Begin,
            audio_time_s: 12.34,
            sample_idx: 296_160,
            attrs: serde_json::Map::new(),
        };
        let line = serde_json::to_string(&rec).unwrap();
        assert!(!line.contains('\n'), "a JSONL record must be single-line");
        assert!(line.contains("\"phase\":\"begin\""), "got {line}");
        assert!(
            !line.contains("attrs"),
            "empty attrs must be omitted to keep records small: {line}"
        );
        let back: TraceRecord = serde_json::from_str(&line).unwrap();
        assert_eq!(back, rec);
    }

    #[test]
    fn trace_record_keeps_non_empty_attrs() {
        let mut attrs = serde_json::Map::new();
        attrs.insert("ttft_source".to_string(), serde_json::json!("push_start"));
        let rec = TraceRecord {
            t_us: 10,
            turn: 0,
            span: "llm.ttft".to_string(),
            phase: Phase::Point,
            audio_time_s: 0.0,
            sample_idx: 0,
            attrs,
        };
        let back: TraceRecord = serde_json::from_str(&serde_json::to_string(&rec).unwrap()).unwrap();
        assert_eq!(back, rec);
    }
}
