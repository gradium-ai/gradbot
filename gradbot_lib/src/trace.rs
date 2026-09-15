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

use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

/// Default record-channel capacity. Large enough that normal sessions never
/// drop, small enough to bound memory if the writer stalls.
const TRACE_CHANNEL_CAPACITY: usize = 8192;

struct TracerInner {
    t0: std::time::Instant,
    tx: tokio::sync::mpsc::Sender<TraceRecord>,
    dropped: AtomicU64,
}

/// Non-blocking span recorder. Cloning is cheap; a disabled tracer costs an
/// `Option` check per call.
#[derive(Clone)]
pub struct Tracer(Option<Arc<TracerInner>>);

/// Handle for reading back records written by an in-memory tracer.
pub struct TraceCollector {
    handle: tokio::task::JoinHandle<Vec<TraceRecord>>,
}

impl TraceCollector {
    /// Waits for the writer task to drain and returns everything it saw.
    /// All `Tracer` clones must be dropped first, or this waits forever.
    pub async fn records(self) -> Vec<TraceRecord> {
        self.handle.await.expect("trace collector task panicked")
    }
}

impl Tracer {
    /// A tracer that records nothing. Used everywhere tracing is not enabled.
    pub fn disabled() -> Self {
        Self(None)
    }

    pub fn in_memory() -> (Self, TraceCollector) {
        Self::with_capacity(TRACE_CHANNEL_CAPACITY)
    }

    pub fn with_capacity(capacity: usize) -> (Self, TraceCollector) {
        let (tx, mut rx) = tokio::sync::mpsc::channel::<TraceRecord>(capacity);
        let handle = tokio::spawn(async move {
            let mut out = Vec::new();
            while let Some(rec) = rx.recv().await {
                out.push(rec);
            }
            out
        });
        let inner = TracerInner {
            t0: std::time::Instant::now(),
            tx,
            dropped: AtomicU64::new(0),
        };
        (Self(Some(Arc::new(inner))), TraceCollector { handle })
    }

    /// Number of records dropped because the channel was full.
    pub fn dropped(&self) -> u64 {
        match &self.0 {
            None => 0,
            Some(inner) => inner.dropped.load(Ordering::Relaxed),
        }
    }

    /// Records one span boundary. Synchronous and non-blocking by contract:
    /// if the channel is full the record is dropped and counted rather than
    /// stalling the session loop.
    pub fn record(
        &self,
        turn: u64,
        span: &str,
        phase: Phase,
        audio_time_s: f64,
        sample_idx: u64,
        attrs: serde_json::Map<String, serde_json::Value>,
    ) {
        let Some(inner) = &self.0 else { return };
        let rec = TraceRecord {
            t_us: inner.t0.elapsed().as_micros() as u64,
            turn,
            span: span.to_string(),
            phase,
            audio_time_s,
            sample_idx,
            attrs,
        };
        if inner.tx.try_send(rec).is_err() {
            inner.dropped.fetch_add(1, Ordering::Relaxed);
        }
    }

    pub fn begin(
        &self,
        turn: u64,
        span: &str,
        audio_time_s: f64,
        sample_idx: u64,
        attrs: serde_json::Map<String, serde_json::Value>,
    ) {
        self.record(turn, span, Phase::Begin, audio_time_s, sample_idx, attrs);
    }

    pub fn end(
        &self,
        turn: u64,
        span: &str,
        audio_time_s: f64,
        sample_idx: u64,
        attrs: serde_json::Map<String, serde_json::Value>,
    ) {
        self.record(turn, span, Phase::End, audio_time_s, sample_idx, attrs);
    }

    pub fn point(
        &self,
        turn: u64,
        span: &str,
        audio_time_s: f64,
        sample_idx: u64,
        attrs: serde_json::Map<String, serde_json::Value>,
    ) {
        self.record(turn, span, Phase::Point, audio_time_s, sample_idx, attrs);
    }

    /// A tracer writing JSONL to `path`. The file is created (and its parent
    /// directory too) if absent. Writing happens on a dedicated task so the
    /// session loop never touches the filesystem.
    pub fn to_file(path: &std::path::Path) -> anyhow::Result<Self> {
        use anyhow::Context;
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("trace: creating {}", parent.display()))?;
        }
        let file = std::fs::File::create(path)
            .with_context(|| format!("trace: creating {}", path.display()))?;

        let (tx, mut rx) = tokio::sync::mpsc::channel::<TraceRecord>(TRACE_CHANNEL_CAPACITY);
        tokio::spawn(async move {
            use std::io::Write;
            let mut file = std::io::BufWriter::new(file);
            while let Some(rec) = rx.recv().await {
                match serde_json::to_string(&rec) {
                    Ok(line) => {
                        if let Err(e) = writeln!(file, "{line}") {
                            tracing::warn!(?e, "trace: write failed, stopping tracer");
                            break;
                        }
                    }
                    Err(e) => tracing::warn!(?e, "trace: serialization failed"),
                }
            }
            if let Err(e) = file.flush() {
                tracing::warn!(?e, "trace: final flush failed");
            }
        });

        let inner = TracerInner {
            t0: std::time::Instant::now(),
            tx,
            dropped: AtomicU64::new(0),
        };
        Ok(Self(Some(Arc::new(inner))))
    }
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

    #[tokio::test]
    async fn tracer_stamps_are_monotonic_and_ordered() {
        let (tracer, collector) = Tracer::in_memory();
        for i in 0..50u64 {
            tracer.point(i, "probe", 0.0, i * 1920, serde_json::Map::new());
        }
        drop(tracer);
        let recs = collector.records().await;
        assert_eq!(recs.len(), 50);
        // Arrow of time: records arrive in emission order with non-decreasing stamps.
        for w in recs.windows(2) {
            assert!(
                w[0].t_us <= w[1].t_us,
                "stamps went backwards: {} then {}",
                w[0].t_us,
                w[1].t_us
            );
            assert!(w[0].turn < w[1].turn, "records must preserve emission order");
        }
    }

    #[tokio::test]
    async fn disabled_tracer_records_nothing_and_drops_nothing() {
        let tracer = Tracer::disabled();
        for i in 0..1000 {
            tracer.point(i, "probe", 0.0, 0, serde_json::Map::new());
        }
        assert_eq!(tracer.dropped(), 0);
    }

    #[tokio::test]
    async fn tracer_drops_rather_than_blocks_when_full_and_counts_drops() {
        // A tiny capacity with no reader draining it: the session loop must never
        // stall on instrumentation, so excess records are dropped and counted.
        let (tracer, _collector) = Tracer::with_capacity(4);
        for i in 0..1000u64 {
            tracer.point(i, "probe", 0.0, 0, serde_json::Map::new());
        }
        assert!(
            tracer.dropped() > 0,
            "expected drops with a capacity-4 channel and 1000 records"
        );
    }

    #[tokio::test]
    async fn begin_and_end_record_the_matching_phases() {
        let (tracer, collector) = Tracer::in_memory();
        tracer.begin(1, "llm.push", 1.0, 100, serde_json::Map::new());
        tracer.end(1, "llm.push", 2.0, 200, serde_json::Map::new());
        drop(tracer);
        let recs = collector.records().await;
        assert_eq!(recs[0].phase, Phase::Begin);
        assert_eq!(recs[1].phase, Phase::End);
        assert_eq!(recs[0].span, "llm.push");
    }

    #[tokio::test]
    async fn to_file_writes_one_json_object_per_line() {
        let dir = std::env::temp_dir().join(format!("gradbot-trace-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("trace.jsonl");

        let tracer = Tracer::to_file(&path).unwrap();
        tracer.begin(0, "llm.push", 1.0, 100, serde_json::Map::new());
        tracer.end(0, "llm.push", 2.0, 200, serde_json::Map::new());
        drop(tracer);

        // Give the writer task a moment to drain and flush after the channel closes.
        for _ in 0..100 {
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
            if std::fs::read_to_string(&path).map(|s| s.lines().count()).unwrap_or(0) >= 2 {
                break;
            }
        }

        let body = std::fs::read_to_string(&path).unwrap();
        let lines: Vec<&str> = body.lines().collect();
        assert_eq!(lines.len(), 2, "expected 2 records, got: {body:?}");
        let first: TraceRecord = serde_json::from_str(lines[0]).unwrap();
        assert_eq!(first.span, "llm.push");
        assert_eq!(first.phase, Phase::Begin);
        std::fs::remove_dir_all(&dir).ok();
    }
}
