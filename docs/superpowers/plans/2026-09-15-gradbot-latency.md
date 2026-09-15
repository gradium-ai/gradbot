# Gradbot Latency Measurement & Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a wall-clock, causally-ordered latency profiler for gradbot's STT → LLM → TTS pipeline, then use it to land and verify optimizations.

**Architecture:** A monotonic per-session `Instant` clock feeds a non-blocking span recorder that writes JSONL to a side channel (never the client WebSocket, to avoid perturbing the path being measured). A real-time benchmark client records the client-side end-to-end number. The two sides join on cumulative input sample index, so no clock synchronization is ever needed. Optimizations land only after the profiler exists.

**Tech Stack:** Rust (edition 2024), tokio, serde/serde_json, tokio-tungstenite, kaudio (Opus), tracing.

**Spec:** `docs/superpowers/specs/2026-09-15-gradbot-latency-design.md` — read it first. It carries the eight findings (A–H) that the tasks below reference by letter.

## Global Constraints

- **Baseline commit:** `db64d77` (gradbot 0.10.5). Branch: `wip/2026-09-15-latency-profiling`.
- **`cargo` is not on `PATH`.** Every Rust command in this plan must be preceded by:
  `export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"`
- **A cold build needs two extra env vars** (discovered in Task 4; pre-existing,
  unrelated to this plan). A warm `target/` does not, so this only bites after a
  fresh clone or `cargo clean` — where the failure is confusing and looks like
  your own change broke the build:
  ```bash
  export CMAKE_POLICY_VERSION_MINIMUM=3.5   # audiopus_sys vendored opus vs CMake 4.4.3
  export LIBRARY_PATH=/usr/lib/python3.12/config-3.12-x86_64-linux-gnu  # gradbot_py -lpython3.12
  ```
- **Rust edition 2024**, workspace-level dependency versions only (`{ workspace = true }`). Do not add a new third-party crate without saying so explicitly; every task here is achievable with crates already in `Cargo.toml`.
- **Tests are inline `#[cfg(test)] mod tests`** at the bottom of the file under test. This is the repo convention (`llm.rs:901`, `mock.rs:415`). Do not create a `tests/` directory for Rust code.
- **Instrumentation must never block or allocate heavily on the session hot path.** Use `try_send` on a bounded channel and count drops. Instrumentation that perturbs the path it measures is worse than none.
- **Real traces may contain unclosed spans, by design.** Of the three Begin/End
  pairs, `llm.push` and `tts.connect` close on their error paths (End carries
  `attrs: {"error": true}`), but `endpoint.flush` does **not** close when a user
  interrupts mid-flush — the state machine leaves `Flushing` and the End never
  fires. That is a legitimately abandoned turn, not a bug; forcing a synthetic
  End would mean threading cleanup through every exit from `Flushing` to satisfy
  a tidiness property. Two consequences: `assert_well_formed` stays strict and so
  any integration test driving a real session must drive a turn that **completes
  normally**; and the Task 13 join must treat a missing End as "this stage was
  not measured for this turn" and say so — never as a zero duration, which would
  silently understate latency for exactly the turns that went wrong.
- **Never stamp a value by subtracting across two different clocks.** This is the bug class behind Findings G and H.
- **Backends:** STT/TTS on prod `https://api.gradium.ai/api`; LLM on self-hosted vLLM, Gemma 4 31B dense (`gemma_31b`) with its `-assistant` draft model.
- **`LLM_BASE_URL` must end in `/v1` with no trailing slash** (`llm.rs:345` appends `/chat/completions` directly).
- **Never report a single run.** Prod backend jitter makes one sample meaningless; always N repetitions with p50/p90/p99.
- **Never commit** audio fixtures >10 MB, `.env`, or API keys. `~/.venv` holds `GRADIUM_API_KEY` and is sourced, never copied into the repo.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `gradbot_lib/src/trace.rs` | **Create.** Monotonic clock, `TraceRecord`, `Phase`, `Tracer`, sinks, drop counting. Self-contained; no knowledge of the pipeline. |
| `gradbot_lib/src/multiplex.rs` | **Modify.** Thread `Tracer` through `start_session`/`run`/`Session`; emit spans at nine instrumentation points. |
| `gradbot_lib/src/lib.rs` | **Modify.** Declare `pub mod trace`; re-export; add `GradbotClients::start_session_traced`. |
| `gradbot_lib/src/speech_to_text.rs` | **Modify (phase 5).** Make the VAD end-of-turn threshold configurable. |
| `gradbot_server/src/config.rs`, `server.rs` | **Modify.** `trace_dir` config; construct the file-backed `Tracer`. |
| `examples/gradbot-bench.rs` | **Create.** Real-time fixture player, client-side marks, repetitions, percentiles. |
| `examples/bench/fixtures.rs` | **Create.** Fixture manifest types + parsing. Separate from the player so it is unit-testable without a socket. |
| `examples/bench/report.rs` | **Create.** Trace join, per-turn decomposition, waterfall + percentile rendering. |
| `docs/superpowers/fixtures/*.json` | **Create.** Fixture manifests (WAV files live outside the repo). |

Phases 1–3 build the profiler and change no pipeline behaviour. Phase 4 lands optimizations. Phase 5 adds knobs and sweeps.

---

# Phase 1 — Clock, span recorder, trace file

### Task 1: Trace record types and JSONL round-trip

**Files:**
- Create: `gradbot_lib/src/trace.rs`
- Modify: `gradbot_lib/src/lib.rs` (add `pub mod trace;` alongside the other module declarations near line 86)

**Interfaces:**
- Consumes: nothing.
- Produces: `TraceRecord { t_us: u64, turn: u64, span: String, phase: Phase, audio_time_s: f64, sample_idx: u64, attrs: serde_json::Map<String, serde_json::Value> }`; `Phase::{Begin, End, Point}` serializing as lowercase strings. Every later task in phase 1 writes these.

- [ ] **Step 1: Write the failing test**

Create `gradbot_lib/src/trace.rs` containing only the test module for now:

```rust
//! Wall-clock span tracing for latency profiling.
//!
//! One monotonic `Instant` per session; every stamp is microseconds since that
//! origin. Causal ordering therefore holds by construction. Records go to a
//! side channel (a JSONL file), never to the client WebSocket — shipping them
//! in-band would add traffic to the exact path being measured.

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
```

Add to `gradbot_lib/src/lib.rs`, in the module declaration block (keep alphabetical placement between `mod system_prompt;` and `pub mod text_to_speech;`):

```rust
pub mod trace;
```

- [ ] **Step 2: Run test to verify it fails**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test -p gradbot trace:: 2>&1 | tail -20
```

Expected: FAIL — `cannot find type TraceRecord in this scope`.

- [ ] **Step 3: Write minimal implementation**

Add above the test module in `gradbot_lib/src/trace.rs`:

```rust
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test -p gradbot trace:: 2>&1 | tail -20
```

Expected: PASS, 2 tests.

- [ ] **Step 5: Commit**

```bash
git add gradbot_lib/src/trace.rs gradbot_lib/src/lib.rs
git commit -m "feat(trace): add TraceRecord and Phase types"
```

---

### Task 2: Tracer with in-memory sink, monotonic stamps, drop counting

**Files:**
- Modify: `gradbot_lib/src/trace.rs`

**Interfaces:**
- Consumes: `TraceRecord`, `Phase` from Task 1.
- Produces:
  - `Tracer` — `Clone`, cheap to clone, safe to hold across `.await`.
  - `Tracer::disabled() -> Tracer` — every call a no-op.
  - `Tracer::in_memory() -> (Tracer, TraceCollector)`
  - `Tracer::record(&self, turn: u64, span: &str, phase: Phase, audio_time_s: f64, sample_idx: u64, attrs: serde_json::Map<String, serde_json::Value>)` — **synchronous, non-blocking, never `.await`**.
  - `Tracer::point/begin/end` convenience wrappers with the same parameters minus `phase`.
  - `Tracer::dropped(&self) -> u64`
  - `TraceCollector::records(&self) -> Vec<TraceRecord>` (async; waits for the writer to drain).

- [ ] **Step 1: Write the failing test**

Add to the `tests` module in `gradbot_lib/src/trace.rs`:

```rust
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test -p gradbot trace:: 2>&1 | tail -20
```

Expected: FAIL — `cannot find type Tracer in this scope`.

- [ ] **Step 3: Write minimal implementation**

Add to `gradbot_lib/src/trace.rs`:

```rust
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
}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test -p gradbot trace:: 2>&1 | tail -20
```

Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add gradbot_lib/src/trace.rs
git commit -m "feat(trace): non-blocking Tracer with monotonic stamps and drop counting"
```

---

### Task 3: File-backed tracer

**Files:**
- Modify: `gradbot_lib/src/trace.rs`

**Interfaces:**
- Consumes: `Tracer`, `TraceRecord` from Tasks 1–2.
- Produces: `Tracer::to_file(path: &std::path::Path) -> anyhow::Result<Tracer>` — writes one JSON object per line. Used by `gradbot_server` in Task 9.

- [ ] **Step 1: Write the failing test**

Add to the `tests` module:

```rust
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test -p gradbot trace::tests::to_file 2>&1 | tail -20
```

Expected: FAIL — `no function or associated item named to_file`.

- [ ] **Step 3: Write minimal implementation**

Add to the `impl Tracer` block:

```rust
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test -p gradbot trace:: 2>&1 | tail -20
```

Expected: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
git add gradbot_lib/src/trace.rs
git commit -m "feat(trace): file-backed JSONL tracer"
```

---

### Task 4: Thread the tracer through the session and instrument the input path

This is the first task that touches the pipeline. It adds the `audio_in.frame` span, which supplies `I` (the server-side anchor) and measures Finding D.

**Files:**
- Modify: `gradbot_lib/src/multiplex.rs` — `start_session` (line 1318), `run` (line 1354), `Session` struct (line ~126) and `Session::new`, the `recv_loop` audio branch (line ~1560)
- Modify: `gradbot_lib/src/lib.rs` — re-export `Tracer`; add `GradbotClients::start_session_traced`

**Interfaces:**
- Consumes: `Tracer` from Task 2.
- Produces:
  - `multiplex::start_session(tts, stt, llm, initial_config, io_format, tracer: Tracer)` — **the `tracer` parameter is appended last**; all existing callers pass `Tracer::disabled()`.
  - `GradbotClients::start_session_traced(config, io_format, tracer)`; the existing `start_session` delegates with `Tracer::disabled()` so `gradbot_py` is untouched.
  - Span `audio_in.frame`, `Phase::Point`, emitted once per decoded input frame with `attrs: {"samples": <n>}`.

- [ ] **Step 1: Write the failing test**

Add a new test module at the bottom of `gradbot_lib/src/multiplex.rs`:

```rust
#[cfg(test)]
mod trace_tests {
    use crate::trace::{Phase, TraceRecord, Tracer};

    /// Every span a task in this plan emits must be well-formed: begin/end pairs
    /// nest per (turn, span), and stamps never go backwards.
    pub(crate) fn assert_well_formed(recs: &[TraceRecord]) {
        for w in recs.windows(2) {
            assert!(w[0].t_us <= w[1].t_us, "stamps went backwards");
        }
        let mut open: std::collections::HashMap<(u64, String), u64> =
            std::collections::HashMap::new();
        for r in recs {
            let key = (r.turn, r.span.clone());
            match r.phase {
                Phase::Begin => {
                    let prev = open.insert(key.clone(), r.t_us);
                    assert!(prev.is_none(), "span {key:?} began twice without ending");
                }
                Phase::End => {
                    let began = open.remove(&key).unwrap_or_else(|| {
                        panic!("span {key:?} ended without beginning")
                    });
                    assert!(r.t_us >= began, "span {key:?} ended before it began");
                }
                Phase::Point => {}
            }
        }
        assert!(open.is_empty(), "unclosed spans: {:?}", open.keys());
    }

    #[tokio::test]
    async fn input_frames_are_traced() {
        let (tracer, collector) = Tracer::in_memory();
        // Emit what the recv_loop emits, to pin the span name and attrs shape.
        let mut attrs = serde_json::Map::new();
        attrs.insert("samples".to_string(), serde_json::json!(1920));
        tracer.point(0, "audio_in.frame", 0.08, 1920, attrs);
        drop(tracer);

        let recs = collector.records().await;
        assert_eq!(recs.len(), 1);
        assert_eq!(recs[0].span, "audio_in.frame");
        assert_eq!(recs[0].sample_idx, 1920);
        assert_eq!(recs[0].attrs.get("samples").unwrap(), &serde_json::json!(1920));
        assert_well_formed(&recs);
    }

    #[test]
    fn well_formed_rejects_unclosed_spans() {
        let rec = TraceRecord {
            t_us: 1,
            turn: 0,
            span: "llm.push".to_string(),
            phase: Phase::Begin,
            audio_time_s: 0.0,
            sample_idx: 0,
            attrs: serde_json::Map::new(),
        };
        let result = std::panic::catch_unwind(|| assert_well_formed(&[rec]));
        assert!(result.is_err(), "an unclosed span must fail the check");
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test -p gradbot trace_tests 2>&1 | tail -20
```

Expected: FAIL — `unresolved import crate::trace` is already satisfied, so this fails at the *next* step instead if written first. If it passes, that is fine: these two tests pin the shape and the real wiring below is what Step 3 delivers.

- [ ] **Step 3: Write the implementation**

1. In `gradbot_lib/src/multiplex.rs`, add `use crate::trace::Tracer;` to the imports at the top.

2. Add a field to `struct Session` (near `stt_connected_at`):

```rust
    /// Span recorder. Disabled tracers make every call a no-op.
    tracer: Tracer,
```

3. Add `tracer: Tracer` as the final parameter of `Session::new`, and set `tracer,` in the struct literal it returns.

4. Change the signature of `start_session` (line 1318) to append the parameter, and pass it to `run`:

```rust
pub async fn start_session(
    tts_client: Arc<TtsClient>,
    stt_client: Arc<SttClient>,
    llm: Arc<crate::llm::Llm>,
    initial_config: Option<SessionConfig>,
    io_format: crate::IoFormat,
    tracer: Tracer,
) -> Result<(SessionInputHandle, SessionOutputHandle)> {
```

and in the `tokio::spawn(run(...))` call add `tracer,` as the final argument.

5. Change `async fn run` (line 1354) to accept `tracer: Tracer` as its final parameter, and pass it into `Session::new`. Clone it for the `recv_loop` before the loop is constructed:

```rust
    let recv_tracer = tracer.clone();
```

6. In `recv_loop`'s `MsgIn::Audio` branch, replace:

```rust
                    let audio = decoder.decode(&audio)?;
                    sender.send_audio(&audio).await?
```

with:

```rust
                    let audio = decoder.decode(&audio)?;
                    sender.send_audio(&audio).await?;
                    // Supplies `I` — the server-side anchor the benchmark joins
                    // on — and measures the 80ms input quantization (Finding D).
                    if !audio.is_empty() {
                        let sample_idx = sender.0.lock().await.samples_sent;
                        let mut attrs = serde_json::Map::new();
                        attrs.insert("samples".to_string(), serde_json::json!(audio.len()));
                        recv_tracer.point(
                            0,
                            "audio_in.frame",
                            sample_idx as f64 / INPUT_SAMPLE_RATE as f64,
                            sample_idx,
                            attrs,
                        );
                    }
```

7. In `gradbot_lib/src/lib.rs`, add to the re-export block near line 102:

```rust
pub use trace::{Phase, TraceRecord, Tracer};
```

8. In `gradbot_lib/src/lib.rs`, change `GradbotClients::start_session` (line 976) to delegate, and add the traced variant:

```rust
    pub async fn start_session(
        &self,
        session_config: Option<SessionConfig>,
        io_format: IoFormat,
    ) -> Result<(SessionInputHandle, SessionOutputHandle)> {
        self.start_session_traced(session_config, io_format, Tracer::disabled())
            .await
    }

    /// As [`Self::start_session`], but records latency spans to `tracer`.
    pub async fn start_session_traced(
        &self,
        session_config: Option<SessionConfig>,
        io_format: IoFormat,
        tracer: Tracer,
    ) -> Result<(SessionInputHandle, SessionOutputHandle)> {
        start_session(
            self.tts.clone(),
            self.stt.clone(),
            self.llm.clone(),
            session_config,
            io_format,
            tracer,
        )
        .await
    }
```

Match the exact field names used in the existing `start_session` body at `lib.rs:981` — read it before editing rather than assuming `self.tts`/`self.stt`.

9. Update the two remaining direct callers to pass `gradbot::Tracer::disabled()` as the final argument: `gradbot_server/src/server.rs:72` and `src/openai_server.rs:203`.

- [ ] **Step 4: Verify the whole workspace builds and tests pass**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo build --workspace 2>&1 | tail -20
cargo test --workspace 2>&1 | tail -30
```

Expected: build clean; all tests pass, including the pre-existing `mock.rs` tests.

- [ ] **Step 5: Commit**

```bash
git add -u
git commit -m "feat(trace): thread Tracer through session, instrument input frames"
```

---

### Task 5: Instrument endpointing (`stt.text`, `endpoint.vad_eot`, `endpoint.flush`)

Measures Finding B directly and supplies `V`.

**Files:**
- Modify: `gradbot_lib/src/multiplex.rs` — `on_text` (line ~1015), `on_end_of_turn` (line ~803), `on_step` (line ~880)

**Interfaces:**
- Consumes: `Session.tracer` from Task 4.
- Produces: spans `stt.text` (Point, attrs `{"stt_start_s": f64, "chars": usize}`), `endpoint.vad_eot` (Point, attrs `{"inactivity_prob": f64}`), `endpoint.flush` (Begin at flush send / End when the gate is satisfied, attrs on Begin `{"flush_duration_s": f64}`). Task 14 reads all three.

- [ ] **Step 1: Write the failing test**

Add to the `trace_tests` module in `multiplex.rs`:

```rust
#[tokio::test]
async fn endpoint_flush_span_is_a_well_formed_pair() {
    let (tracer, collector) = Tracer::in_memory();
    let mut attrs = serde_json::Map::new();
    attrs.insert("flush_duration_s".to_string(), serde_json::json!(0.5));
    tracer.begin(1, "endpoint.flush", 3.0, 72000, attrs);
    tracer.end(1, "endpoint.flush", 3.5, 84000, serde_json::Map::new());
    drop(tracer);

    let recs = collector.records().await;
    assert_well_formed(&recs);
    assert_eq!(recs[0].span, "endpoint.flush");
    assert_eq!(
        recs[0].attrs.get("flush_duration_s").unwrap(),
        &serde_json::json!(0.5)
    );
    // The gate cost (Finding B) is the span's duration.
    assert!(recs[1].t_us >= recs[0].t_us);
}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test -p gradbot trace_tests::endpoint_flush 2>&1 | tail -20
```

Expected: PASS on the shape assertions (it exercises `Tracer` only). This test pins the contract; Step 3 makes the pipeline honour it. Confirm it compiles and passes before proceeding.

- [ ] **Step 3: Write the implementation**

1. In `on_text`, immediately after the existing `tracing::info!(... "on_text called")`:

```rust
        let mut attrs = serde_json::Map::new();
        attrs.insert("stt_start_s".to_string(), serde_json::json!(stt_time));
        attrs.insert("chars".to_string(), serde_json::json!(text.len()));
        let turn = self.state.lock().await.turn_idx();
        let sample_idx = self.stt_sender.0.lock().await.samples_sent;
        self.tracer.point(turn, "stt.text", stt_time, sample_idx, attrs);
```

2. In `on_step`, at the top of the function (before the VAD-interruption block), emit the end-of-turn point when `_end_of_turn` is set. First rename the parameter `_end_of_turn` to `end_of_turn` (it is currently unused; this uses it):

```rust
        if end_of_turn {
            let mut attrs = serde_json::Map::new();
            attrs.insert(
                "inactivity_prob".to_string(),
                serde_json::json!(inactivity_prob),
            );
            let turn = self.state.lock().await.turn_idx();
            let sample_idx = self.stt_sender.0.lock().await.samples_sent;
            self.tracer
                .point(turn, "endpoint.vad_eot", stt_time, sample_idx, attrs);
        }
```

3. In `on_end_of_turn`, immediately after `self.stt_sender.send_flush(flush_duration_s).await?;`:

```rust
            let mut attrs = serde_json::Map::new();
            attrs.insert(
                "flush_duration_s".to_string(),
                serde_json::json!(flush_duration_s),
            );
            let sample_idx = self.stt_sender.0.lock().await.samples_sent;
            self.tracer
                .begin(turn_idx, "endpoint.flush", stt_time, sample_idx, attrs);
```

4. In `on_step`, inside the `State::Flushing` branch where the gate `stt_time - since_s > *flush_duration_s` is satisfied — immediately after `let turn_idx = *turn_idx;` and before `drop(state)`:

```rust
                let flush_end_turn = turn_idx;
```

then after `drop(state);` and before `let _ = self.send_event(Event::EndOfTurn).await;`:

```rust
                let sample_idx = self.stt_sender.0.lock().await.samples_sent;
                self.tracer.end(
                    flush_end_turn,
                    "endpoint.flush",
                    stt_time,
                    sample_idx,
                    serde_json::Map::new(),
                );
```

- [ ] **Step 4: Verify**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo build --workspace 2>&1 | tail -20
cargo test --workspace 2>&1 | tail -30
```

Expected: build clean, tests pass, **no `unused variable: end_of_turn` warning** (confirming the rename is actually used).

- [ ] **Step 5: Commit**

```bash
git add -u
git commit -m "feat(trace): instrument STT text, VAD end-of-turn, and flush gate"
```

---

### Task 6: Instrument the LLM and fix Finding G

The existing `ttft_ms` measures headers→first-token because `llm_request_start` is set *after* `push()` has already awaited the full POST. This task moves the clock origin and records the true interval.

**Files:**
- Modify: `gradbot_lib/src/multiplex.rs` — `llm_tts` (lines ~466–640)

**Interfaces:**
- Consumes: `Session.tracer`.
- Produces: spans `llm.push` (Begin/End around the `push()` await), `llm.ttft` (Point, attrs `{"ttft_from_push_ms": u128, "ttft_from_headers_ms": u128}`), `llm.complete` (Point, attrs `{"total_ms": u128}`). Task 14 reads these.

- [ ] **Step 1: Write the failing test**

Add to `trace_tests`:

```rust
#[tokio::test]
async fn llm_ttft_records_both_origins() {
    let (tracer, collector) = Tracer::in_memory();
    tracer.begin(2, "llm.push", 5.0, 120000, serde_json::Map::new());
    tracer.end(2, "llm.push", 5.1, 122400, serde_json::Map::new());
    let mut attrs = serde_json::Map::new();
    attrs.insert("ttft_from_push_ms".to_string(), serde_json::json!(180u128));
    attrs.insert("ttft_from_headers_ms".to_string(), serde_json::json!(90u128));
    tracer.point(2, "llm.ttft", 5.2, 124800, attrs);
    drop(tracer);

    let recs = collector.records().await;
    assert_well_formed(&recs);
    let ttft = recs.iter().find(|r| r.span == "llm.ttft").unwrap();
    let from_push = ttft.attrs.get("ttft_from_push_ms").unwrap().as_u64().unwrap();
    let from_headers = ttft.attrs.get("ttft_from_headers_ms").unwrap().as_u64().unwrap();
    // Finding G: the pre-existing metric is the smaller, misleading one.
    assert!(
        from_push >= from_headers,
        "TTFT from push must include dispatch and so be >= TTFT from headers"
    );
}
```

- [ ] **Step 2: Run test to verify it compiles and passes**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test -p gradbot trace_tests::llm_ttft 2>&1 | tail -20
```

Expected: PASS. This pins the attrs contract; Step 3 makes the pipeline produce it.

- [ ] **Step 3: Write the implementation**

In `llm_tts`, **before** the `let streaming_session = match self.llm.write().await.push(...)` block (currently around line 464), add:

```rust
        // Finding G: `push()` awaits the full HTTP POST and returns only on
        // response headers, so the clock for a true TTFT must start here —
        // before the push — not after it.
        let llm_push_start = std::time::Instant::now();
        {
            let sample_idx = self.stt_sender.0.lock().await.samples_sent;
            self.tracer.begin(
                turn_idx,
                "llm.push",
                sample_idx as f64 / INPUT_SAMPLE_RATE as f64,
                sample_idx,
                serde_json::Map::new(),
            );
        }
```

Immediately **after** that `match` block completes (after `streaming_session` is bound), add:

```rust
        {
            let sample_idx = self.stt_sender.0.lock().await.samples_sent;
            self.tracer.end(
                turn_idx,
                "llm.push",
                sample_idx as f64 / INPUT_SAMPLE_RATE as f64,
                sample_idx,
                serde_json::Map::new(),
            );
        }
```

Then clone what the spawned task needs, next to the existing `let llm_request_start = std::time::Instant::now();` at line 488 (**keep** that line — it remains the headers-origin clock, now clearly labelled):

```rust
        let llm_tracer = self.tracer.clone();
```

Inside the spawned task's `first_word` branch, replace the existing TTFT logging block with:

```rust
                                    if first_word {
                                        let from_push = llm_push_start.elapsed().as_millis();
                                        let ttft_ms = llm_request_start.elapsed().as_millis();
                                        tracing::info!(
                                            ttft_from_push_ms = from_push,
                                            ttft_from_headers_ms = ttft_ms,
                                            "LLM time-to-first-token"
                                        );
                                        let mut attrs = serde_json::Map::new();
                                        attrs.insert(
                                            "ttft_from_push_ms".to_string(),
                                            serde_json::json!(from_push),
                                        );
                                        attrs.insert(
                                            "ttft_from_headers_ms".to_string(),
                                            serde_json::json!(ttft_ms),
                                        );
                                        llm_tracer.point(turn_idx, "llm.ttft", 0.0, 0, attrs);
                                        let time_s = stt_sender.current_time_s().await;
                                        msg_out_tx
                                            .send(MsgOut::Event { time_s, event: Event::FirstWord })
                                            .await?;
                                        first_word = false;
                                    }
```

`llm_push_start` is `Copy`, so it moves into the async block without a clone.

Where the stream completes (`let llm_total_ms = llm_request_start.elapsed().as_millis();`), add after the existing `tracing::info!`:

```rust
                        let mut attrs = serde_json::Map::new();
                        attrs.insert("total_ms".to_string(), serde_json::json!(llm_total_ms));
                        llm_tracer.point(turn_idx, "llm.complete", 0.0, 0, attrs);
```

- [ ] **Step 4: Verify**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo build --workspace 2>&1 | tail -20
cargo test --workspace 2>&1 | tail -30
```

Expected: build clean, tests pass.

- [ ] **Step 5: Commit**

```bash
git add -u
git commit -m "fix(trace): measure LLM TTFT from push start, not response headers

The logged ttft_ms started its clock after push() had already awaited the
full HTTP POST, silently excluding request dispatch and prefill start.
Both intervals are now recorded so the old number stays comparable."
```

---

### Task 7: Instrument TTS (`tts.connect`, `tts.first_text`, `tts.first_audio`)

Measures Finding A directly.

**Files:**
- Modify: `gradbot_lib/src/multiplex.rs` — the spawned task in `llm_tts` (around line 513)

**Interfaces:**
- Consumes: `llm_tracer` from Task 6 (reuse the same clone).
- Produces: spans `tts.connect` (Begin/End around `tts_stream()`), `tts.first_text` (Point), `tts.first_audio` (Point). Task 14 reads these; Task 17 verifies `tts.connect` shrinks.

- [ ] **Step 1: Write the failing test**

Add to `trace_tests`:

```rust
#[tokio::test]
async fn tts_connect_is_a_well_formed_pair_and_precedes_first_text() {
    let (tracer, collector) = Tracer::in_memory();
    tracer.begin(1, "tts.connect", 0.0, 0, serde_json::Map::new());
    tracer.end(1, "tts.connect", 0.0, 0, serde_json::Map::new());
    tracer.point(1, "tts.first_text", 0.0, 0, serde_json::Map::new());
    tracer.point(1, "tts.first_audio", 0.0, 0, serde_json::Map::new());
    drop(tracer);

    let recs = collector.records().await;
    assert_well_formed(&recs);
    let connect_end = recs.iter().position(|r| r.span == "tts.connect" && r.phase == Phase::End).unwrap();
    let first_text = recs.iter().position(|r| r.span == "tts.first_text").unwrap();
    assert!(
        connect_end < first_text,
        "text cannot be sent before the TTS stream is connected"
    );
}
```

- [ ] **Step 2: Run test**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test -p gradbot trace_tests::tts_connect 2>&1 | tail -20
```

Expected: PASS (contract pinned).

- [ ] **Step 3: Write the implementation**

In the spawned task, wrap the TTS stream creation:

```rust
                tracing::info!(?voice_id, "creating TTS stream");
                let tts_model_name = std::env::var("GRADIUM_TTS_MODEL_NAME").ok();
                // Finding A: a fresh, unpooled WebSocket handshake to prod on
                // every turn, serialized behind the LLM request.
                llm_tracer.begin(turn_idx, "tts.connect", 0.0, 0, serde_json::Map::new());
                let (mut tts_tx, tts_rx) =
                    tts_client.tts_stream(tts_model_name, voice_id.clone(), padding_bonus, rewrite_rules.clone(), tts_extra_config.as_deref()).await
                        .context("TTS: failed to create stream")?;
                llm_tracer.end(turn_idx, "tts.connect", 0.0, 0, serde_json::Map::new());
                tracing::info!("TTS stream created successfully");
```

Clone the tracer for each of the two inner futures, next to the existing clones:

```rust
                let llm_to_tts_tracer = llm_tracer.clone();
                let tts_to_client_tracer = llm_tracer.clone();
```

In `llm_to_tts`, add a `first_text_sent` flag next to `let mut first_word = true;`:

```rust
                        let mut first_text_sent = true;
```

and immediately after each of the two `tts_tx.send_text(...)` calls inside the `while let Some(item)` loop, add:

```rust
                                            if first_text_sent {
                                                llm_to_tts_tracer.point(
                                                    turn_idx,
                                                    "tts.first_text",
                                                    0.0,
                                                    0,
                                                    serde_json::Map::new(),
                                                );
                                                first_text_sent = false;
                                            }
```

In `tts_to_client`, inside the `if first_audio && stop_s > 0.0` block, before the existing `msg_out_tx.send(...)`:

```rust
                                        tts_to_client_tracer.point(
                                            turn_idx,
                                            "tts.first_audio",
                                            0.0,
                                            0,
                                            serde_json::Map::new(),
                                        );
```

- [ ] **Step 4: Verify**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo build --workspace 2>&1 | tail -20
cargo test --workspace 2>&1 | tail -30
```

- [ ] **Step 5: Commit**

```bash
git add -u
git commit -m "feat(trace): instrument TTS connect, first text, and first audio"
```

---

### Task 8: Instrument the output path (`out.encode`, `out.first_audio`)

Measures Finding E and supplies `O`.

**Files:**
- Modify: `gradbot_lib/src/multiplex.rs` — `out_send_loop` (around line 1408)

**Interfaces:**
- Consumes: `Tracer` (clone it for `out_send_loop` in `run`, as `out_tracer`).
- Produces: span `out.encode` (Point, attrs `{"pcm_in": usize, "bytes_out": usize}`) per encode call, and `out.first_audio` (Point) for the first non-empty encoded packet of each turn. **`out.first_audio` supplies `O` and must not fire for Opus header packets.**

- [ ] **Step 1: Write the failing test**

Add to `trace_tests`:

```rust
#[tokio::test]
async fn out_first_audio_fires_once_per_turn_and_never_for_empty_encodes() {
    let (tracer, collector) = Tracer::in_memory();
    // Simulate: a header (0 bytes of real audio) then two real packets.
    for (turn, bytes_out, is_first_real) in [(1usize, 0usize, false), (1, 240, true), (1, 240, false)] {
        let mut attrs = serde_json::Map::new();
        attrs.insert("pcm_in".to_string(), serde_json::json!(3840));
        attrs.insert("bytes_out".to_string(), serde_json::json!(bytes_out));
        tracer.point(turn as u64, "out.encode", 0.0, 0, attrs);
        if is_first_real {
            tracer.point(turn as u64, "out.first_audio", 0.0, 0, serde_json::Map::new());
        }
    }
    drop(tracer);

    let recs = collector.records().await;
    let firsts: Vec<_> = recs.iter().filter(|r| r.span == "out.first_audio").collect();
    assert_eq!(firsts.len(), 1, "out.first_audio must fire exactly once per turn");
    assert_well_formed(&recs);
}
```

- [ ] **Step 2: Run test**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test -p gradbot trace_tests::out_first_audio 2>&1 | tail -20
```

Expected: PASS (contract pinned).

- [ ] **Step 3: Write the implementation**

In `run`, before `out_send_loop` is defined, add:

```rust
    let out_tracer = tracer.clone();
```

In `out_send_loop`, add a per-turn flag next to `let mut last_encoder_turn_idx: u64 = 0;`:

```rust
            let mut first_audio_turn: Option<u64> = None;
```

Replace the encode block in the `TtsOut::Audio` arm:

```rust
                        let pcm_in = pcm.len();
                        let encoded = encoder.encode(&pcm)?;
                        let mut attrs = serde_json::Map::new();
                        attrs.insert("pcm_in".to_string(), serde_json::json!(pcm_in));
                        attrs.insert(
                            "bytes_out".to_string(),
                            serde_json::json!(encoded.data.len()),
                        );
                        // Finding E: Opus buffers until a full page, so empty
                        // results here are the 80ms output quantization.
                        out_tracer.point(turn_idx, "out.encode", start_s, 0, attrs);
                        if !encoded.data.is_empty() {
                            if first_audio_turn != Some(turn_idx) {
                                // Supplies `O`. Deliberately after the
                                // is_empty check so Opus header packets, which
                                // carry no audio, never set the anchor.
                                out_tracer.point(
                                    turn_idx,
                                    "out.first_audio",
                                    start_s,
                                    0,
                                    serde_json::Map::new(),
                                );
                                first_audio_turn = Some(turn_idx);
                            }
```

keeping the existing `tracing::debug!` and `msg_out_tx.send(...)` inside that `if` block unchanged.

- [ ] **Step 4: Verify**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo build --workspace 2>&1 | tail -20
cargo test --workspace 2>&1 | tail -30
```

- [ ] **Step 5: Commit**

```bash
git add -u
git commit -m "feat(trace): instrument Opus encode and first outbound audio"
```

---

### Task 9: Enable tracing in gradbot_server

**Files:**
- Modify: `gradbot_server/src/config.rs`, `gradbot_server/src/server.rs`
- Modify: `configs/gradbot.toml`

**Interfaces:**
- Consumes: `Tracer::to_file` (Task 3), `start_session(..., tracer)` (Task 4).
- Produces: one `trace_<unix_nanos>_<counter>.jsonl` per session under `trace_dir`. Task 14 reads these files.

- [ ] **Step 1: Write the failing test**

Add to `gradbot_server/src/config.rs`, at the bottom:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn trace_dir_defaults_to_none() {
        let toml = r#"
log_dir = "/tmp/logs"
instance_name = "gradbot"
addr = "0.0.0.0"
port = 8000
gradium_base_url = "https://api.gradium.ai/api"
"#;
        let cfg: Config = toml::from_str(toml).unwrap();
        assert!(cfg.trace_dir.is_none(), "tracing must be off unless configured");
    }

    #[test]
    fn trace_dir_is_parsed_when_present() {
        let toml = r#"
log_dir = "/tmp/logs"
instance_name = "gradbot"
addr = "0.0.0.0"
port = 8000
gradium_base_url = "https://api.gradium.ai/api"
trace_dir = "/tmp/traces"
"#;
        let cfg: Config = toml::from_str(toml).unwrap();
        assert_eq!(cfg.trace_dir.as_deref(), Some("/tmp/traces"));
    }
}
```

Read `gradbot_server/src/config.rs` first and adjust the TOML bodies above to include every field the real `Config` requires — the two tests must construct a valid config, differing only in `trace_dir`.

- [ ] **Step 2: Run test to verify it fails**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test -p gradbot_server config:: 2>&1 | tail -20
```

Expected: FAIL — `no field trace_dir on type Config`.

- [ ] **Step 3: Write the implementation**

1. In `gradbot_server/src/config.rs`, add to `struct Config`:

```rust
    /// Directory for per-session latency trace JSONL. Tracing is off when unset.
    #[serde(default)]
    pub trace_dir: Option<String>,
```

2. In `gradbot_server/src/server.rs`, replace the `Tracer::disabled()` placeholder from Task 4 at line ~72:

```rust
    let tracer = match &state.trace_dir {
        None => gradbot::Tracer::disabled(),
        Some(dir) => {
            let ts = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos();
            let cnt = state.cnt.load(std::sync::atomic::Ordering::SeqCst);
            let path = std::path::PathBuf::from(dir).join(format!("trace_{ts}_{cnt:06}.jsonl"));
            match gradbot::Tracer::to_file(&path) {
                Ok(t) => {
                    tracing::info!(path = %path.display(), "latency tracing enabled");
                    t
                }
                Err(e) => {
                    tracing::warn!(?e, "failed to open trace file, continuing untraced");
                    gradbot::Tracer::disabled()
                }
            }
        }
    };
    let (input, output) =
        gradbot::start_session(tts, stt, state.llm.clone(), None, io_format, tracer).await?;
```

Propagate `trace_dir` into `AppState` alongside the existing `log_dir` field; follow exactly how `log_dir` and `log_sessions` are already threaded.

3. In `configs/gradbot.toml`, add a commented line documenting the knob:

```toml
# trace_dir = "$HOME/tmp/gradbot-traces"   # per-session latency spans (JSONL)
```

- [ ] **Step 4: Verify**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo build --workspace 2>&1 | tail -20
cargo test --workspace 2>&1 | tail -30
```

Expected: build clean, all tests pass including the two new config tests.

- [ ] **Step 5: Commit**

```bash
git add -u
git commit -m "feat(server): optional per-session latency trace via trace_dir"
```

---

### Task 9b: Wire tracing into the OpenAI-compatible server

**Why this task exists:** Task 9 wired `trace_dir` into `gradbot_server`, which is
the wrong binary. `configs/gradbot.toml` sets `transport = "ws-openai"`, which
drives `gradbot_bin` → `src/openai_server.rs` — and that is the server the
benchmark connects to. The spec's Non-goals scope this work to "the
OpenAI-compatible WebSocket path only (`src/openai_server.rs`)". Without this
task the Phase 2 gate runs against a server whose tracer is permanently
`disabled()` and produces no trace file at all.

**Files:**
- Modify: `src/lib.rs` (the `Config` for `gradbot_bin`), `src/openai_server.rs:212`

**Interfaces:**
- Consumes: `Tracer::to_file` (Task 3), `start_session(..., tracer)` (Task 4).
- Produces: the same per-session `trace_<unix_nanos>_<counter>.jsonl` behaviour
  Task 9 gave `gradbot_server`, on the server the benchmark actually uses.

Mirror Task 9 exactly — the same `trace_dir: Option<String>` with
`#[serde(default)]`, the same `replace_env_vars` treatment, the same dedicated
`AtomicU64` counter claimed with `fetch_add` (never `load` — see Task 9's
Critical finding), and the same degrade-to-disabled-on-open-failure behaviour so
a bad trace path can never kill a voice call. Read Task 9's implementation in
`gradbot_server/src/{config,server}.rs` and follow it rather than re-deriving.

Keep the `gradbot_server` wiring in place; it is a real server and its tracing
is correct, just not the one under benchmark.

---

**Phase 1 gate:** the profiler exists and the pipeline is unchanged in behaviour. Confirm `git diff db64d77 --stat` shows no change to endpointing constants, frame sizes, or connection ordering.

---

# Phase 2 — Benchmark harness and fixtures

### Task 10: Fixture manifest types and parsing

**Files:**
- Create: `examples/bench/fixtures.rs`
- Create: `docs/superpowers/fixtures/smoke.json`

**Interfaces:**
- Consumes: nothing.
- Produces: `Manifest { name: String, turns: Vec<FixtureTurn> }`, `FixtureTurn { wav: PathBuf, speech_start_sample: u64, speech_end_sample: u64, gap_after_s: f64, category: String }`, `Manifest::load(path) -> Result<Manifest>`, `FixtureTurn::validate(&self) -> Result<()>`. Tasks 11 and 16 consume these.

- [ ] **Step 1: Write the failing test**

Create `examples/bench/fixtures.rs`:

```rust
//! Benchmark fixture manifests.
//!
//! Boundaries are known *by construction* — fixtures are concatenated from
//! utterances whose sample offsets we recorded when building them — never
//! inferred by running a VAD over them, which would make the measurement
//! circular.

use anyhow::{Context, Result};
use std::path::{Path, PathBuf};

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_a_manifest() {
        let json = r#"{
          "name": "smoke",
          "turns": [
            {"wav": "a.wav", "speech_start_sample": 0, "speech_end_sample": 24000,
             "gap_after_s": 3.0, "category": "short_answer"}
          ]
        }"#;
        let m: Manifest = serde_json::from_str(json).unwrap();
        assert_eq!(m.name, "smoke");
        assert_eq!(m.turns.len(), 1);
        assert_eq!(m.turns[0].speech_end_sample, 24000);
        assert_eq!(m.turns[0].category, "short_answer");
        m.turns[0].validate().unwrap();
    }

    #[test]
    fn rejects_end_before_start() {
        let turn = FixtureTurn {
            wav: PathBuf::from("a.wav"),
            speech_start_sample: 24000,
            speech_end_sample: 100,
            gap_after_s: 3.0,
            category: "bad".to_string(),
        };
        let err = turn.validate().unwrap_err().to_string();
        assert!(err.contains("speech_end_sample"), "got: {err}");
    }

    #[test]
    fn rejects_non_positive_gap() {
        // The harness must keep streaming silence after each utterance: VAD
        // needs frames to keep ticking and the flush mechanism depends on it.
        let turn = FixtureTurn {
            wav: PathBuf::from("a.wav"),
            speech_start_sample: 0,
            speech_end_sample: 100,
            gap_after_s: 0.0,
            category: "bad".to_string(),
        };
        let err = turn.validate().unwrap_err().to_string();
        assert!(err.contains("gap_after_s"), "got: {err}");
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test --example gradbot-bench 2>&1 | tail -20
```

Expected: FAIL — the example target does not exist yet. That is the expected failure; Task 11 creates it. To run this task's tests in isolation before then, temporarily add `#[path = "bench/fixtures.rs"] mod fixtures;` to `examples/gradbot-client.rs` — remove it once Task 11 lands.

- [ ] **Step 3: Write the implementation**

Add above the test module in `examples/bench/fixtures.rs`:

```rust
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct FixtureTurn {
    /// 24 kHz mono WAV. Resampled by the harness if it is not already 24 kHz.
    pub wav: PathBuf,
    /// Sample offset within `wav` where speech begins.
    pub speech_start_sample: u64,
    /// Sample offset where speech ends. This is the ground truth the headline
    /// metric and `premature_cut_rate` are both measured against.
    pub speech_end_sample: u64,
    /// Silence streamed after this turn before the next one starts.
    pub gap_after_s: f64,
    /// Fixture category, e.g. "short_answer", "mid_sentence_pause".
    pub category: String,
}

impl FixtureTurn {
    pub fn validate(&self) -> Result<()> {
        if self.speech_end_sample <= self.speech_start_sample {
            anyhow::bail!(
                "speech_end_sample ({}) must exceed speech_start_sample ({}) in {}",
                self.speech_end_sample,
                self.speech_start_sample,
                self.wav.display()
            );
        }
        if self.gap_after_s <= 0.0 {
            anyhow::bail!(
                "gap_after_s must be positive in {} — the harness must keep \
                 streaming silence so VAD keeps ticking",
                self.wav.display()
            );
        }
        Ok(())
    }
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct Manifest {
    pub name: String,
    pub turns: Vec<FixtureTurn>,
}

impl Manifest {
    pub fn load(path: &Path) -> Result<Self> {
        let body = std::fs::read_to_string(path)
            .with_context(|| format!("reading manifest {}", path.display()))?;
        let manifest: Self = serde_json::from_str(&body)
            .with_context(|| format!("parsing manifest {}", path.display()))?;
        for turn in &manifest.turns {
            turn.validate()?;
        }
        Ok(manifest)
    }
}
```

Create `docs/superpowers/fixtures/smoke.json` with one turn pointing at a WAV path outside the repo (WAV files are **not** committed):

```json
{
  "name": "smoke",
  "turns": [
    {
      "wav": "/data/home/neil/datasets/gradbot-bench/short_yes.wav",
      "speech_start_sample": 0,
      "speech_end_sample": 12000,
      "gap_after_s": 4.0,
      "category": "short_answer"
    }
  ]
}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test --example gradbot-client fixtures:: 2>&1 | tail -20
```

Expected: PASS, 3 tests.

- [ ] **Step 5: Commit**

```bash
git add examples/bench/fixtures.rs docs/superpowers/fixtures/smoke.json
git commit -m "feat(bench): fixture manifest types with boundary validation"
```

---

### Task 11: Benchmark client — real-time playback and client-side marks

**Files:**
- Create: `examples/gradbot-bench.rs`
- Modify: `Cargo.toml` (declare the example if examples are not auto-discovered — check first; `examples/*.rs` is auto-discovered by cargo, so this is likely a no-op)

**Interfaces:**
- Consumes: `Manifest`, `FixtureTurn` (Task 10).
- Produces: `ClientMark { turn: u64, kind: MarkKind, t_us: u64, sample_idx: u64 }`, `MarkKind::{UserSpeechEnd, FirstAgentAudio}`, and `fn e2e_ms(marks: &[ClientMark], turn: u64) -> Option<f64>`. Task 14 consumes the emitted marks JSON. **`turn` is `u64` throughout, matching `TraceRecord.turn`** — Task 13 compares the two directly, so a `usize` here would not compile.

- [ ] **Step 1: Write the failing test**

Create `examples/gradbot-bench.rs` with the marks types and this test module:

```rust
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test --example gradbot-bench 2>&1 | tail -20
```

Expected: FAIL — `cannot find type ClientMark`.

- [ ] **Step 3: Write the implementation**

Write `examples/gradbot-bench.rs`. Copy the WebSocket connect/split scaffolding and the real-time send pacing verbatim from `examples/gradbot-client.rs` — that pacing (`sleep_until` against a fixed origin, never cumulative sleeps) is already correct and must not be re-derived. On top of it add:

```rust
#[path = "bench/fixtures.rs"]
mod fixtures;

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
```

The player loop must:
- hold one `std::time::Instant` origin for the whole run and stamp every mark from it;
- stream each turn's WAV at 24 kHz in 1920-sample chunks, pacing with `sleep_until(origin + samples/24000)`;
- emit `UserSpeechEnd` at the moment the chunk containing `speech_end_sample` is written, recording the cumulative `sample_idx` at that point;
- then stream `gap_after_s` of silence in the same chunking, never stopping the stream between turns;
- on the receive side, decode `ResponseAudioDelta` and emit `FirstAgentAudio` for the first packet of each turn where `is_agent_audio(&decoded)`;
- write all marks as JSON to `--out-marks`.

CLI: `--url`, `--manifest`, `--repetitions` (default 1), `--out-marks`.

- [ ] **Step 4: Run test to verify it passes**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test --example gradbot-bench 2>&1 | tail -20
cargo build --examples 2>&1 | tail -10
```

Expected: PASS, 3 tests; examples build.

- [ ] **Step 5: Commit**

```bash
git add examples/gradbot-bench.rs Cargo.toml
git commit -m "feat(bench): real-time fixture player with client-side latency marks"
```

---

### Task 12: Repetitions and percentiles

**Files:**
- Modify: `examples/gradbot-bench.rs`

**Interfaces:**
- Consumes: `e2e_ms` (Task 11).
- Produces: `fn percentile(sorted: &[f64], p: f64) -> f64`, `struct Summary { n: usize, p50: f64, p90: f64, p99: f64 }`, `fn summarize(values: &[f64]) -> Option<Summary>`, and `async fn probe_rtt_ms(ws: &mut WebSocket, n: usize) -> Result<Summary>`. Task 14 renders the summaries; the **phase 3 gate depends on `probe_rtt_ms`** to confirm the computed `network_ms` is physically plausible.

- [ ] **Step 1: Write the failing test**

Add to the test module in `examples/gradbot-bench.rs`:

```rust
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test --example gradbot-bench percentile 2>&1 | tail -20
```

Expected: FAIL — `cannot find function percentile`.

- [ ] **Step 3: Write the implementation**

```rust
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
```

Add the transport probe and its plausibility check:

```rust
/// Whether a computed `network_ms` residual is physically believable given an
/// independently measured round trip. A negative residual means the join is
/// wrong; a residual far above the RTT means server time went unattributed.
/// Either way the breakdown is not trustworthy and must be fixed before any
/// optimization decision rests on it.
pub fn residual_is_plausible(network_ms: f64, rtt_p50_ms: f64) -> bool {
    network_ms >= 0.0 && network_ms <= rtt_p50_ms * 4.0 + 50.0
}

/// Measures WebSocket round-trip time with `n` ping/pong exchanges.
pub async fn probe_rtt_ms(ws: &mut WebSocket, n: usize) -> anyhow::Result<Summary> {
    use futures_util::{SinkExt, StreamExt};
    let mut samples = Vec::with_capacity(n);
    for i in 0..n {
        let started = std::time::Instant::now();
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
```

Call `probe_rtt_ms(&mut ws, 20)` once per run, before the fixture playback begins so it never competes with measured traffic, and record it in the marks output.

Wire `--repetitions` to run the manifest N times, opening a **fresh session per repetition and never more than one at a time**, accumulating `e2e_ms` per turn category.

- [ ] **Step 4: Run test to verify it passes**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test --example gradbot-bench 2>&1 | tail -20
```

Expected: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
git add -u
git commit -m "feat(bench): repetitions, percentiles, and a transport RTT probe"
```

**Phase 2 gate — first real measurement, AND the profiler's first real proof of life.**

> **Why this gate carries extra weight.** Phase 1 closes with *no* in-process
> evidence that spans are ever emitted. An integration test was attempted and
> found impossible without restructuring: `multiplex::start_session` takes
> concrete `Arc<TtsClient>` / `Arc<SttClient>` / `Arc<Llm>`, `multiplex.rs` never
> references `mock`, and `mock.rs` never drives `Session`/`run` — there is no
> trait seam. Creating one means making the session generic across ~30+ call
> sites, which would break this plan's own "phases 1-3 change no pipeline
> behaviour" rule. So every Phase 1 test pins span *shapes* via the `Tracer` API;
> all of them would still pass if the instrumentation were deleted outright.
>
> **Therefore, before trusting any number from this gate, verify the trace file
> itself:**
> 1. A trace file exists for the session and is **non-empty**. An empty or
>    missing file means the instrumentation never ran — stop and fix that first.
> 2. It contains at least `audio_in.frame`, `endpoint.vad_eot`, `llm.push`,
>    `tts.connect`, and `out.first_audio`. A missing span name means that call
>    site is dead.
> 3. Every `t_us` is non-decreasing in file order.
> 4. `tracer.dropped()` is 0. Non-zero means records were lost and the
>    measurement is incomplete — raise the channel capacity and re-run.
> 5. **Pin the Opus header assumption with a real-audio test.** The harness
>    excludes Opus header packets from "first agent audio" by checking that
>    decoded PCM is non-empty. That this holds rests on `kaudio`'s decoder
>    returning an empty slice for an `OpusHead`/`OpusTags`-only packet —
>    verified by reading the crate's source, but pinned by no test in this
>    repo, so a future `kaudio` bump could break it silently and the harness
>    would start reporting near-zero latencies that look plausible. Once real
>    fixture audio exists, add a test that decodes an actual captured server
>    response and asserts the first packet is rejected and the second accepted.
>
> Record the outcome in the ledger. This is the check Phase 1 could not perform.

Requires the vLLM job and credentials:

```bash
cd ~/code/audium
uv run python -m scripts.vllm.launch gemma_31b --registry ~/endpoints/gemma31 --wait
cat ~/endpoints/gemma31/endpoints.jsonl

source ~/.venv
export LLM_BASE_URL="http://<host>:<port>/v1"
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo run --release --bin gradbot_bin -- --config configs/gradbot.toml &
cargo run --release --example gradbot-bench -- \
  --url ws://localhost:8000/v1/realtime \
  --manifest docs/superpowers/fixtures/smoke.json \
  --repetitions 20 --out-marks /tmp/marks.json
```

Confirm the p50 is stable across two independent 20-repetition runs before trusting anything downstream. Record both baselines in the commit message.

---

# Phase 3 — Waterfall report

### Task 13: Join client marks to the server trace

**Files:**
- Create: `examples/bench/report.rs`

**Interfaces:**
- Consumes: `TraceRecord` (Task 1), `ClientMark` (Task 11).
- Produces: `TurnBreakdown { turn: u64, e2e_ms: f64, detection_lag_ms: f64, server_internal_ms: f64, network_ms: f64, spans: Vec<(String, f64, f64)> }` and `fn breakdown(marks: &[ClientMark], trace: &[TraceRecord], turn: u64) -> Result<TurnBreakdown>`.

- [ ] **Step 1: Write the failing test**

```rust
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
            ClientMark { turn: 1, kind: MarkKind::UserSpeechEnd, t_us: 9_000_000, sample_idx: 24_000 },
            ClientMark { turn: 1, kind: MarkKind::FirstAgentAudio, t_us: 9_900_000, sample_idx: 0 },
        ];

        let b = breakdown(&marks, &trace, 1).unwrap();
        assert_eq!(b.e2e_ms, 900.0);
        assert_eq!(b.detection_lag_ms, 60.0);   // V - I
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
            ClientMark { turn: 1, kind: MarkKind::UserSpeechEnd, t_us: 0, sample_idx: 24_000 },
            ClientMark { turn: 1, kind: MarkKind::FirstAgentAudio, t_us: 900_000, sample_idx: 0 },
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
            ClientMark { turn: 1, kind: MarkKind::UserSpeechEnd, t_us: 0, sample_idx: 24_000 },
            ClientMark { turn: 1, kind: MarkKind::FirstAgentAudio, t_us: 900_000, sample_idx: 0 },
        ];
        // Client turn 1 vs server turns 7 and 9 — none of them equal.
        let b = breakdown(&marks, &trace, 1).unwrap();
        assert_eq!(b.detection_lag_ms, 60.0);
        assert_eq!(b.server_internal_ms, 800.0);
    }

    #[test]
    fn missing_anchor_is_an_error_not_a_zero() {
        let trace = vec![rec(100_000, 1, "out.first_audio", Phase::Point, 0)];
        let marks = vec![
            ClientMark { turn: 1, kind: MarkKind::UserSpeechEnd, t_us: 0, sample_idx: 24_000 },
            ClientMark { turn: 1, kind: MarkKind::FirstAgentAudio, t_us: 900_000, sample_idx: 0 },
        ];
        assert!(breakdown(&marks, &trace, 1).is_err());
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test --example gradbot-bench report:: 2>&1 | tail -20
```

Expected: FAIL — `cannot find function breakdown`.

- [ ] **Step 3: Write the implementation**

Implement `breakdown` following the spec's decomposition exactly.

**Join by ordering and sample index — never by turn equality.** The client's
`turn` counts fixture turns; the server's `turn_idx` is its own sequence that
also advances on interruptions, and the OpenAI-compatible protocol carries no
turn id for the client to adopt. The two numberings are therefore unrelated, and
matching them would silently mis-attribute every stage after the first
interruption. Anchor on the sample index (which both sides genuinely share) and
then follow causal order:

- `I` = `t_us` of the **first** `audio_in.frame` whose `sample_idx >= mark.sample_idx` (frames advance in 1920-sample steps, so an exact match is not guaranteed).
- `V` = `t_us` of the first `endpoint.vad_eot` with `t_us >= I` — by time, not by turn.
- `O` = `t_us` of the first `out.first_audio` with `t_us >= V` — by time, not by turn.

The `turn` field on trace records stays useful for grouping spans within the
waterfall render, but it must not participate in the client↔server join.
- `e2e_ms = (first_agent_audio.t_us - user_speech_end.t_us) / 1000`
- `detection_lag_ms = (V - I) / 1000`
- `server_internal_ms = (O - I) / 1000`
- `network_ms = e2e_ms - server_internal_ms`

Return `anyhow::Error` naming the missing span when any anchor is absent — never silently substitute zero, which would understate latency.

Populate `spans` with `(name, start_ms, end_ms)` relative to `I` for every Begin/End pair in the turn, so overlapping spans (`tts.connect` racing the LLM stream) render correctly as a Gantt rather than being summed.

- [ ] **Step 4: Run test to verify it passes**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test --example gradbot-bench 2>&1 | tail -20
```

Expected: PASS, 9 tests.

- [ ] **Step 5: Commit**

```bash
git add examples/bench/report.rs
git commit -m "feat(bench): join client marks to server trace by sample index"
```

---

### Task 14: Render the waterfall report

**Files:**
- Modify: `examples/bench/report.rs`, `examples/gradbot-bench.rs`

**Interfaces:**
- Consumes: `TurnBreakdown` (Task 13), `Summary` (Task 12).
- Produces: `fn render_markdown(breakdowns: &[TurnBreakdown], summaries: &BTreeMap<String, Summary>, llm_local: bool) -> String`.

- [ ] **Step 1: Write the failing test**

```rust
#[test]
fn report_labels_the_llm_stage_as_measured_local() {
    let b = TurnBreakdown {
        turn: 1,
        e2e_ms: 900.0,
        detection_lag_ms: 60.0,
        server_internal_ms: 800.0,
        network_ms: 100.0,
        spans: vec![("llm.push".to_string(), 10.0, 60.0)],
    };
    let out = render_markdown(&[b], &std::collections::BTreeMap::new(), true);
    assert!(
        out.contains("measured-local"),
        "a locally-hosted LLM flatters its own stage and must be labelled: {out}"
    );
}

#[test]
fn report_states_the_repetition_count() {
    let mut summaries = std::collections::BTreeMap::new();
    summaries.insert(
        "short_answer".to_string(),
        Summary { n: 20, p50: 800.0, p90: 950.0, p99: 1100.0 },
    );
    let out = render_markdown(&[], &summaries, false);
    assert!(out.contains("n=20"), "every aggregate must state its n: {out}");
}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test --example gradbot-bench report::tests::report 2>&1 | tail -20
```

Expected: FAIL — `cannot find function render_markdown`.

- [ ] **Step 3: Write the implementation**

Render, in order: a percentile table (one row per fixture category, each stating `n=`), then one ASCII Gantt per turn with bars positioned by `(start_ms, end_ms)` relative to `I`, then the `detection_lag / server_internal / network` split. When `llm_local` is true, append `(measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)` to every LLM row.

Wire `--out-report` into `gradbot-bench` to write the rendered markdown.

- [ ] **Step 4: Run test to verify it passes**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test --example gradbot-bench 2>&1 | tail -20
```

Expected: PASS, 11 tests.

- [ ] **Step 5: Commit**

```bash
git add -u
git commit -m "feat(bench): waterfall report with measured-local LLM labelling"
```

**Phase 3 gate — the profiler is trustworthy or it is not.** Run 20 repetitions and assert `residual_is_plausible(network_ms, rtt_p50_ms)` (Task 12) holds for every turn. A negative `network_ms` means the sample-index join is wrong; one far above the RTT means server time is going unattributed. Either way, **stop and fix it before phase 4** — every optimization decision downstream rests on this number being real.

---

# Phase 4 — Land the free wins

Each optimization lands in its own commit with a before/after measurement. **Any change whose improvement does not exceed run-to-run spread gets reverted**, not kept on the grounds that it "should" help.

### Task 15: Fix A — overlap the TTS handshake with the LLM request

**Files:**
- Modify: `gradbot_lib/src/multiplex.rs` — `llm_tts`

**Interfaces:** unchanged. Only ordering changes.

- [ ] **Step 1: Record the baseline**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo run --release --example gradbot-bench -- --url ws://localhost:8000/v1/realtime \
  --manifest docs/superpowers/fixtures/smoke.json --repetitions 20 \
  --out-marks /tmp/before-marks.json --out-report /tmp/before.md
grep -A3 "tts.connect" /tmp/before.md
```

Write down the `tts.connect` p50 and the overall p50. If `tts.connect` p50 is under 20 ms, **stop — Finding A is not worth fixing**; record that in the commit message and skip to Task 16.

- [ ] **Step 2: Write the implementation**

Move `tts_client.tts_stream(...)` so it runs concurrently with the LLM request rather than after it. The TTS stream needs only `voice_id`, `padding_bonus`, `rewrite_rules` and `tts_extra_config` — none of which depend on the LLM — so start it before `llm.push()` and join:

```rust
        let tts_connect = {
            let tts_client = self.tts_client.clone();
            let voice_id = voice_id.clone();
            let rewrite_rules = rewrite_rules.clone();
            let tts_extra_config = tts_extra_config.clone();
            let tts_model_name = std::env::var("GRADIUM_TTS_MODEL_NAME").ok();
            async move {
                tts_client
                    .tts_stream(
                        tts_model_name,
                        voice_id,
                        padding_bonus,
                        rewrite_rules,
                        tts_extra_config.as_deref(),
                    )
                    .await
            }
        };
```

then `tokio::join!` it with the push. Hoist the `voice_id` read out of the spawned task to before the join — it currently happens inside the task, and the join needs it earlier. Keep the `tts.connect` span begin/end around the future so the measurement stays comparable.

- [ ] **Step 3: Verify tests still pass**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test --workspace 2>&1 | tail -20
```

- [ ] **Step 4: Measure after, and decide**

```bash
cargo run --release --example gradbot-bench -- --url ws://localhost:8000/v1/realtime \
  --manifest docs/superpowers/fixtures/smoke.json --repetitions 20 \
  --out-marks /tmp/after-marks.json --out-report /tmp/after.md
```

Keep the change only if the overall p50 improves by more than the spread between the two independent baselines recorded at the phase 2 gate. Otherwise `git checkout -- gradbot_lib/src/multiplex.rs` and record the negative result.

- [ ] **Step 5: Commit**

```bash
git add -u
git commit -m "perf(tts): overlap TTS handshake with the LLM request

Before: p50 <X>ms, tts.connect p50 <Y>ms. After: p50 <Z>ms (n=20)."
```

- [ ] **Step 6: Decide whether pre-warming is warranted — do not implement it by default**

Spec §8 step 2 (pre-opening a TTS stream during `Listening`, behind a
`tts_prewarm` knob) is **deliberately deferred and data-gated**. It needs an
idle-timeout and refresh policy, and it burns a connection on every silent
session, so it must earn its place.

Re-read `tts.connect` p50 from `/tmp/after.md`. Pre-warming is warranted **only
if, after this task's overlap fix, `tts.connect` is still on the critical path** —
that is, `tts.connect` ends *later* than `llm.ttft` in the waterfall, so TTS is
still what first audio is waiting on. If the LLM's first token lands after the
handshake completes, the handshake is fully hidden and pre-warming would buy
nothing.

Record the verdict either way in `docs/superpowers/plans/2026-09-15-sweep-results.md`
(created in Task 20). If warranted, it becomes its own task with its own plan —
do not bolt it on here.

---

### Task 16: Fix F — send the first token to TTS without waiting for a word boundary

**Files:**
- Modify: `gradbot_lib/src/multiplex.rs` — the `llm_to_tts` text branch

- [ ] **Step 1: Write the failing test**

Add to `trace_tests` a unit test for the buffering predicate, extracted as a free function so it is testable without a socket:

```rust
#[test]
fn first_chunk_flushes_immediately_but_later_chunks_wait_for_a_boundary() {
    assert!(should_flush_to_tts("Hel", true), "the first chunk must not wait");
    assert!(!should_flush_to_tts("lo wor", false), "mid-word must wait");
    assert!(should_flush_to_tts("hello ", false), "a word boundary flushes");
    assert!(should_flush_to_tts("hello.", false), "punctuation flushes");
}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test -p gradbot first_chunk_flushes 2>&1 | tail -20
```

Expected: FAIL — `cannot find function should_flush_to_tts`.

- [ ] **Step 3: Write the implementation**

```rust
/// Whether the buffered text should be handed to TTS now.
///
/// The first chunk of a turn flushes immediately: making TTS wait for a word
/// boundary costs a whole extra LLM token before synthesis can even start
/// (Finding F). Later chunks still wait, so "don't" and "well-known" stay
/// intact.
fn should_flush_to_tts(buffer: &str, is_first: bool) -> bool {
    if is_first && !buffer.is_empty() {
        return true;
    }
    matches!(
        buffer.chars().last(),
        Some(' ' | '.' | '!' | '?' | ',' | '\n')
    )
}
```

Use it in the text branch, preserving the existing `pending_numeric` handling — the mid-number stash must still win over the first-chunk rule, or "$50,000" will be split.

- [ ] **Step 4: Measure**

Run the 20-repetition before/after as in Task 15. Keep only if it beats the spread.

- [ ] **Step 5: Commit**

```bash
git add -u
git commit -m "perf(tts): flush the first LLM chunk without awaiting a word boundary"
```

---

### Task 17: Measure D and E, then decide on frame sizes

**Files:**
- Modify: `gradbot_lib/src/multiplex.rs` (`INPUT_FRAME_SIZE` line 52, `OUTPUT_FRAME_SIZE` line 54) — **only if the data justifies it**

- [ ] **Step 1: Read the measurement**

From the phase 3 report, extract the `audio_in.frame` inter-arrival p50 (Finding D) and the fraction of `out.encode` records with `bytes_out == 0` (Finding E).

- [ ] **Step 2: Decide**

If each contributes under 20 ms to the p50, **record that and stop** — halving a frame size doubles per-frame syscall and Opus overhead, and is not free. Write the numbers into the commit message either way.

- [ ] **Step 3: If justified, halve the frame sizes**

```rust
const INPUT_FRAME_SIZE: usize = 960;   // 40ms @ 24kHz (was 1920 = 80ms)
pub const OUTPUT_FRAME_SIZE: usize = 1920; // 40ms @ 48kHz (was 3840 = 80ms)
```

`OUTPUT_FRAME_SIZE` is publicly re-exported (`lib.rs:102`) and read by `examples/gradbot-client.rs` and `gradbot_py`; grep for every use and confirm each still holds before changing it.

- [ ] **Step 4: Verify and measure**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test --workspace 2>&1 | tail -20
```

Then the 20-repetition before/after. Listen to a recorded output to confirm no audible artefacts from the smaller Opus pages — this is the one change in phase 4 that can degrade audio quality.

- [ ] **Step 5: Commit**

```bash
git add -u
git commit -m "perf(audio): halve input/output frame sizes to 40ms

Measured contribution: D <X>ms, E <Y>ms of p50. After: p50 <Z>ms (n=20)."
```

---

# Phase 5 — Knobs and the Pareto sweep

### Task 18: Add the endpointing knobs

**Files:**
- Modify: `gradbot_lib/src/multiplex.rs` (`SessionConfig` line ~1095, `on_end_of_turn` line 845), `gradbot_lib/src/speech_to_text.rs` (line 105)

**Interfaces:**
- Produces: `SessionConfig.min_listen_before_flush_s: f64` (default 0.5) and `SessionConfig.vad_eot_threshold: f64` (default 0.8). Task 20 sweeps both.
- **Not produced here:** the spec's fourth knob, `tts_prewarm`, is gated on Task 15 Step 6 and is only added if the measurement there shows the TTS handshake is still on the critical path after the overlap fix. Adding it unconditionally would be configurability nobody asked for.

- [ ] **Step 1: Write the failing test**

```rust
#[test]
fn endpointing_knobs_default_to_current_hardcoded_values() {
    let c = SessionConfig::default();
    assert_eq!(c.min_listen_before_flush_s, 0.5, "must match multiplex.rs:845");
    assert_eq!(c.vad_eot_threshold, 0.8, "must match speech_to_text.rs:105");
}
```

If `SessionConfig` has no `Default` impl, construct it explicitly in the test with every field and assert on the two new ones instead.

- [ ] **Step 2: Run test to verify it fails**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test -p gradbot endpointing_knobs 2>&1 | tail -20
```

Expected: FAIL — `no field min_listen_before_flush_s`.

- [ ] **Step 3: Write the implementation**

Add both fields to `SessionConfig` with doc comments naming the line they replace. Replace the literal at `multiplex.rs:845`:

```rust
            let min_listen_s = self
                .session_config
                .lock()
                .await
                .as_ref()
                .map_or(0.5, |c| c.min_listen_before_flush_s);
            if stt_time - *since_s <= min_listen_s {
                return Ok(());
            }
```

Thread `vad_eot_threshold` into `SttClient::stt_stream` and use it in place of the `0.8` literal. Update every `SessionConfig` construction site — `gradbot_py/src/lib.rs`, `src/openai_server.rs`, `gradbot_server/src/server.rs` — to set the defaults explicitly; `cargo build --workspace` will name them all.

- [ ] **Step 4: Verify**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test --workspace 2>&1 | tail -30
```

Then run 20 repetitions with the defaults and confirm the p50 is unchanged from phase 4 — **a knob that changes behaviour at its default value is a bug**.

- [ ] **Step 5: Commit**

```bash
git add -u
git commit -m "feat(config): make endpointing thresholds configurable

Defaults match the previous hardcoded values exactly; p50 unchanged (n=20)."
```

---

### Task 19: Turn-taking quality metrics

**Files:**
- Modify: `examples/bench/report.rs`

**Interfaces:**
- Produces: `struct TurnTaking { premature_cut_rate: f64, endpoint_delay_ms: Summary, missed_endpoint_rate: f64 }` and `fn turn_taking(marks: &[ClientMark], trace: &[TraceRecord], manifest: &Manifest) -> TurnTaking`.

- [ ] **Step 1: Write the failing test**

```rust
#[test]
fn agent_audio_before_ground_truth_speech_end_is_a_premature_cut() {
    // The agent answered 100ms before the user had finished speaking.
    let marks = vec![
        ClientMark { turn: 0, kind: MarkKind::UserSpeechEnd, t_us: 1_000_000, sample_idx: 24_000 },
        ClientMark { turn: 0, kind: MarkKind::FirstAgentAudio, t_us: 900_000, sample_idx: 0 },
    ];
    assert!(is_premature_cut(&marks, 0));
}

#[test]
fn answering_after_speech_end_is_not_a_cut() {
    let marks = vec![
        ClientMark { turn: 0, kind: MarkKind::UserSpeechEnd, t_us: 1_000_000, sample_idx: 24_000 },
        ClientMark { turn: 0, kind: MarkKind::FirstAgentAudio, t_us: 1_800_000, sample_idx: 0 },
    ];
    assert!(!is_premature_cut(&marks, 0));
}

#[test]
fn a_turn_with_no_agent_audio_is_a_missed_endpoint_not_a_cut() {
    let marks = vec![ClientMark {
        turn: 0, kind: MarkKind::UserSpeechEnd, t_us: 1_000_000, sample_idx: 24_000,
    }];
    assert!(!is_premature_cut(&marks, 0));
}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test --example gradbot-bench premature 2>&1 | tail -20
```

Expected: FAIL — `cannot find function is_premature_cut`.

- [ ] **Step 3: Write the implementation**

`is_premature_cut` is true when both marks exist for the turn and `FirstAgentAudio.t_us < UserSpeechEnd.t_us`. `endpoint_delay_ms` is `V` minus the trace record whose `sample_idx` matches the turn's ground-truth speech end. `missed_endpoint_rate` is the fraction of manifest turns with no `FirstAgentAudio` mark. Add all three to the rendered report.

- [ ] **Step 4: Run test to verify it passes**

```bash
export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
cargo test --example gradbot-bench 2>&1 | tail -20
```

- [ ] **Step 5: Commit**

```bash
git add -u
git commit -m "feat(bench): turn-taking quality metrics"
```

---

### Task 20: Build the fixture set and sweep the Pareto frontier

**Files:**
- Create: `docs/superpowers/fixtures/turn_taking.json`
- Create: `docs/superpowers/plans/2026-09-15-sweep-results.md`

- [ ] **Step 1: Build the fixture set**

Synthesize the six categories from the spec — short answers, mid-sentence pause, hesitation, digit strings, trailing off, fast back-to-back — recording exact sample boundaries as each is concatenated. WAVs go in `~/datasets/gradbot-bench/`, **not** the repo. Add a small hand-annotated real-speech set as `real_check.json`; the synthetic set is for iteration, the real set decides whether a tuning is trustworthy.

- [ ] **Step 2: Sweep**

For `flush_duration_s` in {0.2, 0.3, 0.4, 0.5, 0.7} × `min_listen_before_flush_s` in {0.2, 0.5}, run 20 repetitions of `turn_taking.json` and record p50 latency against `premature_cut_rate`.

- [ ] **Step 3: Plot and write up**

Write `2026-09-15-sweep-results.md` with the full table, the Pareto-optimal subset, and the same sweep re-run on `real_check.json` at the two or three most promising points. **If the synthetic and real sets disagree, the real set wins** and the disagreement itself is the finding.

- [ ] **Step 4: Present, do not decide**

Choosing the operating point is Neil's call, not the implementer's. Present the frontier and a recommendation.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/fixtures/turn_taking.json docs/superpowers/fixtures/real_check.json \
        docs/superpowers/plans/2026-09-15-sweep-results.md
git commit -m "docs: latency/turn-taking Pareto sweep results"
```

---

## Notes for the executor

- **Phases 1–3 need no credentials.** Everything is unit-testable. Only the phase-2 gate onward needs the vLLM job and `~/.venv`.
- **Findings B and C may be correctly tuned already.** If the sweep shows the current values are Pareto-optimal, that is a successful phase 5, not a failure. Report the curve.
- **Never end a turn with a dirty tree**; push the branch after each commit. Do not merge to `main` — open a PR and ask.
