# Latency lever analysis — 2026-09-19

Measured against live backends: gradbot `ws-openai` on loopback, STT/TTS on prod
`api.gradium.ai`, LLM a co-located vLLM `google/gemma-4-31B-it` + speculative
draft. Fixtures: 12 one-sided turns from `/data/datasets/phonon` across six
categories. Baseline e2e p50 **1546 ms** (n=36).

## Budget

| Block | p50 | Share |
|---|---|---|
| VAD end-of-turn detection | 592–630 ms | ~38% |
| TTS synthesis (first text → first audio) | 380 ms | ~23% |
| Flush hold | ~283 ms wall | ~18% |
| LLM TTFT (push → first token) | 188 ms | ~12% |
| Output encode → socket | 64 ms | ~4% |
| Network + client | 39 ms | ~2% |

## gradbot's own orchestration adds ~nothing

Internal reaction gaps, from traces (n=39–45):

| gap | p50 | p90 |
|---|---|---|
| `vad_eot` → `flush.begin` | 0.1 ms | 0.2 ms |
| `flush.end` → `llm.push.begin` | 0.0 ms | 0.0 ms |
| `tts.first_audio` → `out.first_audio` | 1.2 ms | 1.3 ms |
| **`llm.ttft` → `tts.first_text`** | **45.5 ms** | **296.6 ms** |

Every gap is ~zero except the last: the first LLM token waits in gradbot's
buffer for a word boundary before TTS may start. Across n=161 turns: p50 45.5,
p75 172, p90 297, p95 413 ms; **48% of turns wait >50 ms**. Fixed in `6869f65`
(first chunk flushes immediately; the mid-number stash still takes precedence so
"$50,000" cannot be split).

**Validated against live backends.** Same span pair, measured after the fix:

| | n | p50 | p75 | p90 | turns >50 ms |
|---|---|---|---|---|---|
| before | 161 | 45.5 ms | 172.2 ms | 296.6 ms | 48% |
| after | 15 | **0.0 ms** | **0.0 ms** | **0.1 ms** | **0%** |

The gap is eliminated, not reduced. n=15 is small, but the distribution
collapsed from a long tail to a hard zero — the mechanism working by
construction, not a noisy improvement. Worth ~45 ms at p50 and ~300 ms at p90
against a ~1550 ms budget; the p90 is the real prize.

## Levers closed by measurement

| Lever | Result |
|---|---|
| EOT `delay` conditioning | **Exhausted.** Swept 0.0 / 0.4 / 0.8 / 1.2 on the LiveKit EOT benchmark. 0.8 optimal; curve turns over both sides (cutoff@0.6s: 20.1 / 9.2 / **8.7** / 9.6 %). The script default was already best. |
| VAD head index | **No win.** Prod exposes 4 heads `[0.5, 1.0, 2.0, 3.0]`; gradbot uses index 2. Head 3 at fixed threshold: p50 detection 611 vs 630 ms but p90 948 vs 799, p99 1241 vs 939 — dominated. Head and threshold are coupled and must be swept together. |
| `flush_duration_s` | **Actively harmful.** e2e p50 rose monotonically as it fell: 0.5 → 1546 ms, 0.3 → 1615 ms, 0.2 → 1700 ms. At 0.2 the transcript reaching the LLM also lost 7% of its characters (1092 → 1014 chars/turn on identical audio). The gate is on STT's reported clock, not wall time, so cutting the parameter does not cut the wait proportionally. 77% of flush windows genuinely yield new transcript. |
| TTS synthesis | **No lever from gradbot.** `cfg_coef` is CFG-*distilled* (`tts_backbone.py:332-335` sets `config.cfg_coef = 1.0` and passes strength as a conditioning token), so it changes conditioning, not compute. The real latency knobs (`lookahead_end_of_stream`, `max_lookahead_size`, `second_stream_ahead`) are checkpoint-level, not per-stream. |
| LLM TTFT | **~30 ms available, below the bar.** vLLM prefix caching already works (110 ms cold → 60 ms warm on a ~1400-token prompt; 30 ms on a trivial one). The system prompt costs ~30 ms over nothing. |
| TTS handshake overlap | **~8 ms.** `tts.connect` (181 ms) is already fully hidden inside the TTFT window. |
| Opus output buffering | **0 ms.** 0 of 139 encodes buffered without output. |

## Untested

`delay_in_frames` — an STT `json_config` parameter gradbot never sets. One STT
frame is 80 ms (`sample_rate=24000`, `frame_size=1920`, measured), documented
range 0–80, so 0–6.4 s of lookahead. Our 592–630 ms detection lag is ≈7.9
frames. Confirmed accepted by the API (a session with `{"delay_in_frames": 4}`
ran clean); never quantified, because prod STT stayed at its 20-session cap.
Reachable from the wire since `7a01712`.

## Not gradbot's to fix

- **Deploy `55966eda@500`.** On the LiveKit-comparable EOT benchmark it beats
  prod Gradium on both axes: 897 vs 913 ms mean latency @5% cutoff, 551 vs
  656 ms @10%, and 22.8% vs 55.6% false-cutoff at a 300 ms budget.
- **EP model gap.** LiveKit Turn Detector v1 is ~350 ms ahead at equal cutoff in
  English (543 vs 897 ms @5%). Moshi EP is *ahead* in French (573 vs 598 ms at
  the 600 ms budget). This is model quality, not configuration.

## Operational finding

Dropped clients appear to leak STT sessions. Every benchmark repetition ends
`Connection reset without closing handshake`; sessions accumulate against a
shared 20-session cap and drain only over several minutes. In production a
dropped call would hold a session until timeout, and 20 concurrent is not much
headroom. This blocked the `delay_in_frames` measurement.
