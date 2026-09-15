# Gradbot end-to-end latency: measurement, profiling, optimization

- **Date:** 2026-09-15
- **Status:** design approved, not yet implemented
- **Baseline:** `db64d77` (gradbot 0.10.5)
- **Branch:** `wip/2026-09-15-latency-profiling`

## Problem

Gradbot's STT → LLM → TTS turnaround is slower than it needs to be, and today there
is no way to say by how much or where it goes. The repository contains exactly two
latency measurements, both `tracing::info!` lines in `multiplex.rs`, and one of them
measures the wrong interval (see Finding G). Every other stage — endpointing, TTS
connection setup, Opus framing, transport — is unmeasured.

The goal is to reach a Pareto-optimal point on quality vs. latency for the whole
interaction. That requires measurement first: a wall-clock, causally-ordered
breakdown of a turn, against which optimizations can be justified and regressions
caught.

## Goals

1. A headline end-to-end latency number measured on real wall time from a client's
   vantage point, reproducible across runs.
2. A per-stage attribution of that number precise enough to rank culprits.
3. A turn-taking quality metric that moves in the opposite direction from latency,
   so the endpointing tradeoff can be plotted rather than argued.
4. Optimizations landed against that measurement, each shown to beat run-to-run
   noise.

## Non-goals

- Changing STT, TTS, or LLM model quality. Backends are selected, not modified.
- Optimizing anything downstream of the client socket (playback, jitter buffers).
- Multi-session throughput or concurrency. This is single-session latency work.
- Twilio transport. The OpenAI-compatible WebSocket path only (`src/openai_server.rs`).

## Environment

| Component | Choice | Rationale |
| --- | --- | --- |
| STT | prod `https://api.gradium.ai/api` | measure what users experience |
| TTS | prod `https://api.gradium.ai/api` | same |
| LLM | self-hosted vLLM on gcore, **Gemma 4 31B dense** (`gemma_31b`) | only LLM available (no OpenAI key); makes TTFT A/B testable |
| Cluster | gcore, Slurm | GPUs required for vLLM |
| Toolchain | `cargo` from the `binaries` conda env | not on default `PATH` |

### Bringing the LLM up

```bash
cd ~/code/audium
uv run python -m scripts.vllm.launch gemma_31b --registry ~/endpoints/gemma31 --wait
cat ~/endpoints/gemma31/endpoints.jsonl     # {"host":..., "port":..., "model":...}
```

`gemma_31b` is `google/gemma-4-31B-it` served with the `gemma-4-31B-it-assistant`
draft model for speculative decoding. Serving it without the draft model is not a
supported configuration and would not be a representative TTFT baseline.

```bash
source ~/.venv                                  # GRADIUM_API_KEY
export LLM_BASE_URL="http://<host>:<port>/v1"   # note the /v1, and no trailing slash
```

Three wiring facts, each verified against the source:

- **`/v1` is required.** `llm.rs:345` builds the request URL as
  `format!("{}/chat/completions", base_url)` — it appends directly and inserts
  nothing. This is the inverse of the scripty convention (`base_urls` without
  `/v1`), against the same cluster and the same models. Easy to get backwards.
- **No trailing slash.** `completions_url` is built from the *un-trimmed* base URL
  (the trim at `llm.rs:299` applies only to the `async-openai` client, not to
  `cache_key`), so `.../v1/` yields `//chat/completions`. Cosmetic, but avoid it.
- **`LLM_API_KEY` and `LLM_MODEL` can stay unset.** The key falls through to
  `.unwrap_or_default()` (`llm.rs:285`) and vLLM accepts any bearer; the model is
  auto-detected when the endpoint serves exactly one (`llm.rs:330`), which it does.

### Known bias

The vLLM instance sits on the same cluster as the harness, so its network cost is
approximately zero — unlike any production deployment with a remote LLM. The LLM
stage will therefore look better than it would in production. **The report must
label this stage as measured-local**, or orchestration will be tuned against an LLM
cost that does not exist in the real deployment.

## Findings that motivate the design

All verified against `db64d77`.

| | Finding | Cost |
| --- | --- | --- |
| **A** | TTS WebSocket is opened *inside* the per-turn task (`multiplex.rs:515`), after `push()` has fully returned. A fresh, unpooled WebSocket handshake to prod on **every turn**, serialized behind the LLM request. | 1 RTT + handshake per turn |
| **B** | `flush_duration_s = 0.5` (`multiplex.rs:7`) pushes 0.5 s of zeros into STT (`multiplex.rs:1082`); `on_step` then gates on `stt_time - since_s > flush_duration_s`. STT must process synthetic silence before the LLM is pushed at all. | hard ≥500 ms floor per turn |
| **C** | `on_end_of_turn` returns early when `stt_time - since_s <= 0.5` (`multiplex.rs:845`). A short utterance gets no flush and waits for a later VAD event. | suspected ~1 s on short answers |
| **D** | `INPUT_FRAME_SIZE = 1920` @ 24 kHz = **80 ms** input quantization (`multiplex.rs:52`). | mean +40 ms, worst +80 ms |
| **E** | `OUTPUT_FRAME_SIZE = 3840` @ 48 kHz = **80 ms**; Opus `encode_page` buffers until a full page (`multiplex.rs:54`). | up to +80 ms to first audio |
| **F** | `llm_to_tts` holds the first token until a word boundary arrives. TTS receives nothing until token 2. | +1 token |
| **G** | `llm_request_start` is set at `multiplex.rs:488`, *after* `push()` returns — but `push()` awaits the full HTTP POST (`llm.rs:690`), returning only on response headers. The logged `ttft_ms` is headers→first-token, silently excluding dispatch and prefill start. | existing metric is wrong |
| **H** | `audio_time_s()` is `samples_sent / 24000` and samples advance only in 1920-sample frames, so the clock every existing event is stamped on is itself quantized to 80 ms — and it drifts against the STT clock by seconds over a call (documented at `multiplex.rs:947`). | existing timestamps untrustworthy |

A, D, E, F carry no quality risk. B and C are the endpointing tradeoff and require
the quality guard. G and H are why new instrumentation is needed rather than better
reading of the existing logs.

## Design

### 1. Clock

One `std::time::Instant` per session captured at connect (`t0`). Every stamp is
`(Instant::now() - t0)` in microseconds. `Instant` is monotonic by contract, so
causal ordering holds by construction — that is the "arrow of time" property. **No
stamp is ever produced by subtracting across two different clocks**; that mixing is
the bug class behind Findings G and H.

The existing `audio_time_s()` sample-count clock is **left untouched** — it is
load-bearing for endpointing — but is recorded alongside wall time on every span, so
the drift in Finding H becomes a measured quantity instead of a code comment.

### 2. No clock synchronization

Client and server have unrelated `t0` origins and we deliberately do not align them.
Both sides produce *durations* on their own monotonic clocks, and the decomposition
is built to need nothing more.

Join key between the two sides: **cumulative input sample index**. Both already
count it — `SttSender_::samples_sent` on the server, `samples_sent_so_far` in
`examples/gradbot-client.rs` — and they count the same samples.

Per turn, on the client clock:

- `U` = wall time the last speech sample of the turn was written to the socket
- `A` = wall time the first agent audio packet with non-empty decoded PCM arrived
- **`E2E = A − U`** ← the headline metric

Per turn, on the server clock:

- `I` = wall time the input frame carrying that same sample index was decoded
- `V` = wall time `end_of_turn` was observed
- `O` = wall time the first agent audio byte of the turn was written to the socket

Which gives, with no shared clock:

```
server_internal = O − I          (fully attributable, broken down by span)
detection_lag   = V − I          (STT/VAD endpoint detection, server clock)
network         = E2E − (O − I)  (transport in + out, plus client processing)
```

### 3. Span taxonomy and trace format

Spans are written as JSONL, one record per boundary, to the session log directory
(extending the mechanism already gated behind `log_sessions` at
`gradbot_server/src/server.rs:105`).

```json
{"t_us": 1234567, "turn": 3, "span": "tts.connect", "phase": "begin",
 "audio_time_s": 12.34, "sample_idx": 296160, "attrs": {}}
```

`phase` is `begin` | `end` | `point`. Spans overlap — TTS connect races the LLM
stream — so this is an interval set rendered as a waterfall, **not** a flat sum.

| Span | Captures |
| --- | --- |
| `audio_in.frame` | input quantization (**D**), and supplies `I` |
| `stt.text` | arrival wall time vs. STT-reported `start_s` → STT's own lag |
| `endpoint.vad_eot` | supplies `V` |
| `endpoint.flush` | flush sent → gate satisfied (**B**) |
| `llm.push` | history assembly + POST dispatch — the interval Finding G omits |
| `llm.ttft`, `llm.complete` | true first token, measured from push *start* |
| `tts.connect` | **A**, measured directly |
| `tts.first_text`, `tts.first_audio` | word-boundary hold (**F**), TTS backend time |
| `out.encode`, `out.first_audio` | Opus page buffering (**E**), queueing; supplies `O` |

Recording must be non-blocking and allocation-light on the hot path — a bounded
channel to a writer task, never a synchronous file write inside the session loop.
Instrumentation that perturbs the path it measures is worse than none.

### 4. Why a side-channel and not the `Event` protocol

The `Event` enum is `#[serde(tag = "type")]` and is deserialized by `gradbot_py` and
the demos. Two reasons the trace does not go in-band:

1. **Observer effect.** The useful granularity is per-token and per-packet. Shipping
   that over the same WebSocket adds traffic to the exact path being measured.
2. **Protocol break.** Adding *fields* to the enum would be safe; adding the new
   *variants* this needs would not be.

Correlation does not require in-band data, because the harness runs one session at a
time — a constraint that is independently necessary, since concurrent sessions would
confound measurements against shared prod backends.

### 5. Harness

New `examples/gradbot-bench.rs`, built on the real-time pacing already correct in
`examples/gradbot-client.rs` (`sleep_until` against a fixed origin).

- Reads a fixture manifest: per turn, a WAV plus ground-truth speech start/end
  sample offsets.
- One session at a time. N repetitions. Reports p50/p90/p99 — **never a single run**,
  since prod backend jitter makes one sample meaningless.
- Records `U` and `A` per turn, plus the sample index at `U` for the server join.
- Continues streaming silence after each utterance — VAD needs frames to keep
  ticking, and the flush mechanism depends on it.
- Measures WebSocket ping/pong RTT separately as a transport sanity check against
  the computed `network` residual.

**Opus header packets must be excluded from `A`.** `out_send_loop` emits a header
with `start_s = 0` on every turn change (`multiplex.rs:1436`). Counting it reports a
near-zero latency that is entirely fictional. This is the single easiest way to
produce a fake good number, so the harness counts only packets with non-empty
decoded PCM, and a test asserts this.

### 6. Fixtures and the quality guard

Fixtures are built by concatenation so boundaries are known by construction rather
than inferred. Categories chosen to stress B and C specifically:

- short answers ("yes", "Paris") — stresses **C**
- mid-sentence pause ("I'd like to book… a table for four") — premature-cut trap
- hesitation ("um, let me think")
- digit strings with natural pauses ("four one five… two two…")
- trailing off
- fast back-to-back turns

Metrics, reported alongside latency percentiles:

- `premature_cut_rate` — agent audio begins before ground-truth speech end
- `endpoint_delay` — `V` minus ground-truth speech end
- `missed_endpoint_rate` — no turn triggered within a timeout

**Known weakness.** Synthesizing the user turns with Gradium TTS gives determinism,
exact boundaries and reproducibility, but that speech is cleaner than real speech and
VAD will behave better on it than in production, making endpointing numbers
optimistic. Mitigation: a small hand-annotated real-speech set as a reality check.
The synthetic set is for iteration; the real set decides whether a tuning is
trustworthy.

### 7. Configuration knobs

Deliberately minimal — only what is actually swept. `SessionConfig` already carries
per-session tunables, so this extends an existing pattern.

| Knob | Status | Gates |
| --- | --- | --- |
| `flush_duration_s` | exists | **B** |
| `min_listen_before_flush_s` | new — hardcoded `0.5` at `multiplex.rs:845` | **C** |
| `vad_eot_threshold` | new — hardcoded `0.8` at `speech_to_text.rs:105` | endpointing |
| `tts_prewarm` | new — boolean | **A** |

Frame sizes (**D**, **E**) get measured, not knobbed. If they prove significant they
are a constant change plus a verification run; adding configuration for them would be
speculative flexibility.

Sweeping `flush_duration_s` × `premature_cut_rate` *is* the Pareto frontier, plotted.

### 8. Fixing A incrementally

1. `tokio::join!(llm.push(…), tts_client.tts_stream(…))` — removes the
   serialization. Small, safe. Measure.
2. Only if the handshake still dominates: pre-open a TTS stream during `Listening`.
   This needs an idle-timeout and refresh policy and wastes connections on silent
   sessions, so it must earn its place with data.

### 9. Report

Per-turn waterfall plus an aggregate percentile table. The LLM stage carries a
visible *measured-local* label per the bias noted above, and every aggregate states
its repetition count.

## Phases

Nothing in the pipeline changes until phase 4.

1. **Clock + span recorder + trace file.** Verify: trace emitted for a session;
   a test asserts stamps are monotonic and spans are well-nested.
2. **Harness + fixtures.** Verify: baseline p50/p90 reproducible across repeat runs;
   a test asserts Opus header packets are excluded from `A`.
3. **Waterfall report.** Verify: `server_internal` plus computed `network` accounts
   for `E2E` within the ping/pong transport estimate.
4. **Land A, D, E, F.** Verify: paired runs show improvement beyond run-to-run
   spread. Each lands separately so attribution stays clean.
5. **Sweep B, C against turn-taking metrics.** Verify: a Pareto curve exists and an
   operating point is chosen from it.

## Risks

- **Prod jitter dominating the signal.** Mitigated by repetitions and percentiles,
  and by the per-stage attribution that separates "backend was slow" from "we
  regressed". If variance still swamps the effects in phase 4, the fallback is
  paired interleaved A/B runs rather than sequential ones.
- **Synthetic fixtures flattering the VAD.** Flagged above; mitigated by the
  real-speech check set.
- **Local LLM flattering the LLM stage.** Flagged above; mitigated by labelling.
- **Instrumentation perturbing the measurement.** Mitigated by the side-channel
  decision and the non-blocking writer.
- **Findings B and C are latency floors, not bugs.** They may prove to be correctly
  tuned. The deliverable in that case is the Pareto curve showing so, not a change.
