<!-- First real end-to-end latency baseline. Conditions matter: read them before quoting any number. -->
# First baseline — 2026-09-17

**Setup.** gradbot `ws-openai` on localhost; STT/TTS on prod `api.gradium.ai`;
LLM = self-hosted vLLM `google/gemma-4-31B-it` + speculative draft, co-located on
`gradium-training-gpu-09` (hence the *measured-local* label on every LLM row —
its network cost is ~0, unlike a production remote LLM).
Fixture: one 5.95 s real-speech turn from the `earn63` corpus, trailing silence
trimmed so `speech_end_sample` is exact by construction. n=3 repetitions.

**Caveats.** n=3 is small; the client is on loopback so `network` is ~0 and not
representative; a single fixture turn means these numbers say nothing about
short utterances, hesitations, or mid-sentence pauses — the categories that
stress endpointing hardest.

---

# Gradbot latency report

## Turn measurement coverage

All 3 turn(s) attempted in this run were measured; every aggregate below covers all of them.

## End-to-end latency by fixture category (ms)

| category | n | p50 | p90 | p99 |
| --- | --- | --- | --- | --- |
| smoke_real_speech | n=3 | 1484.56 | 1620.73 | 1620.73 |

## Per-turn waterfalls (3 of 3 turn(s) measured)

### Turn 0 (repetition 0, fixture turn 0, e2e=1620.7ms)

```
[                   ===========                    ]    610.4-949.2   ms  endpoint.flush
[                              =                   ]    949.2-957.6   ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                              ======              ]    957.7-1138.8  ms  tts.connect
```

### Turn 1 (repetition 1, fixture turn 0, e2e=1452.1ms)

```
[                     ==========                   ]    611.1-912.5   ms  endpoint.flush
[                               =                  ]    912.5-921.2   ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                                ======            ]    921.3-1094.7  ms  tts.connect
```

### Turn 2 (repetition 2, fixture turn 0, e2e=1484.6ms)

```
[                    ===========                   ]    586.9-908.3   ms  endpoint.flush
[                               =                  ]    908.3-917.4   ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                               ======             ]    917.5-1090.6  ms  tts.connect
```

## detection_lag / server_internal / network split (ms)

Every row below is over the 3 measured turn(s) of 3 attempted; the 0 unmeasurable turn(s) are in none of them (see *Turn measurement coverage*).

0 of those 3 measured turn(s) had no detection-lag measurement (no `endpoint.vad_eot` anchor found) and are excluded from the `detection_lag` row below as well — a percentile computed over fewer samples than the reader assumes would itself be misleading.

LLM stage note: this run's LLM is (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)

| stage | n | p50 | p90 | p99 |
| --- | --- | --- | --- | --- |
| detection_lag | n=3 | 610.19 | 610.92 | 610.92 |
| server_internal | n=3 | 1484.18 | 1581.34 | 1581.34 |
| network | n=3 | 0.38 | 39.38 | 39.38 |

### network residual plausibility

All 3 measured turn(s) pass `residual_is_plausible` against the measured p50 RTT of 0.05ms.

## Turn-taking quality

These get worse as endpointing is tuned more aggressively — read them alongside the latency numbers above, not instead of them.

- premature_cut_rate: 0.0% (agent's first audio arrived before the user actually finished speaking)
- missed_endpoint_rate: 0.0% (agent never answered the turn at all)

| stage | n | p50 | p90 | p99 |
| --- | --- | --- | --- | --- |
| endpoint_delay_ms | n=3 | 610.19 | 610.92 | 610.92 |
