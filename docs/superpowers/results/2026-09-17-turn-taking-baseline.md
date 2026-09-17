# Turn-taking baseline — 2026-09-17

**Fixtures.** 12 one-sided turns drawn from `/data/datasets/phonon` (the TTS'd
`generalist` assistant dialogue, one voice), across six categories: short_answer,
medium_turn, question, digits, hesitation, long_turn. 2.09 s to 11.50 s.
Each file is a single synthesized turn, so trimming trailing silence makes
`speech_end_sample` exact **by construction** — no VAD was used to define the
ground truth the VAD is being measured against. n=2 repetitions, 24 turns total.

**Why not the earlier earnings-call fixture.** It was one 5.95 s monologue turn
and never exercised `min_listen_before_flush_s`. It also measured ~1484 ms,
optimistic against every category here.

**Why not real full-duplex.** Determining where a user's turn ends in real
conversation needs a VAD or hand annotation; using a VAD to define ground truth
for measuring VAD latency is circular. Synthetic single-turn audio avoids that.
The known cost: TTS speech is cleaner than real speech, so VAD likely performs
better here than in production — these endpointing numbers are a floor.

**Setup.** gradbot ws-openai on loopback; STT/TTS on prod api.gradium.ai;
LLM = co-located vLLM google/gemma-4-31B-it + speculative draft.

---

# Gradbot latency report

## Turn measurement coverage

All 24 turn(s) attempted in this run were measured; every aggregate below covers all of them.

## End-to-end latency by fixture category (ms)

| category | n | p50 | p90 | p99 |
| --- | --- | --- | --- | --- |
| digits | n=4 | 1950.42 | 2112.79 | 2112.79 |
| hesitation | n=4 | 1854.94 | 2102.03 | 2102.03 |
| long_turn | n=4 | 1872.42 | 1873.29 | 1873.29 |
| medium_turn | n=4 | 1601.99 | 1881.16 | 1881.16 |
| question | n=4 | 1866.70 | 1930.04 | 1930.04 |
| short_answer | n=4 | 1777.29 | 2076.49 | 2076.49 |

## Per-turn waterfalls (24 of 24 turn(s) measured)

### Turn 0 (repetition 0, fixture turn 0, e2e=1777.3ms)

```
[                    ==========                    ]    724.0-1064.7  ms  endpoint.flush
[                              =                   ]   1064.7-1072.8  ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                              =========           ]   1072.8-1368.5  ms  tts.connect
```

### Turn 1 (repetition 0, fixture turn 1, e2e=1762.1ms)

```
[                   ========                       ]    640.1-937.1   ms  endpoint.flush
[                           =                      ]    937.1-946.0   ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                           ===========            ]    946.0-1323.4  ms  tts.connect
```

### Turn 2 (repetition 0, fixture turn 2, e2e=1582.6ms)

```
[                  ==========                      ]    558.5-870.6   ms  endpoint.flush
[                            =                     ]    870.6-880.0   ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                             =====                ]    880.0-1048.4  ms  tts.connect
```

### Turn 3 (repetition 0, fixture turn 3, e2e=1560.6ms)

```
[                   ===========                    ]    596.8-904.8   ms  endpoint.flush
[                              =                   ]    904.8-914.5   ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                              ======              ]    914.5-1114.1  ms  tts.connect
```

### Turn 4 (repetition 0, fixture turn 4, e2e=1743.4ms)

```
[                       ========                   ]    798.9-1073.3  ms  endpoint.flush
[                               =                  ]   1073.3-1082.8  ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                                ====              ]   1082.9-1242.6  ms  tts.connect
```

### Turn 5 (repetition 0, fixture turn 5, e2e=1532.9ms)

```
[                     ==========                   ]    624.2-912.4   ms  endpoint.flush
[                               =                  ]    912.4-922.2   ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                               ======             ]    922.3-1103.6  ms  tts.connect
```

### Turn 6 (repetition 0, fixture turn 6, e2e=1950.4ms)

```
[                        =======                   ]    933.2-1174.1  ms  endpoint.flush
[                               =                  ]   1174.1-1182.9  ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                               ====               ]   1183.0-1345.8  ms  tts.connect
```

### Turn 7 (repetition 0, fixture turn 7, e2e=1299.8ms)

```
[                ============                      ]    413.3-711.4   ms  endpoint.flush
[                            =                     ]    711.4-721.0   ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                            =======               ]    721.2-881.8   ms  tts.connect
```

### Turn 8 (repetition 0, fixture turn 8, e2e=1597.9ms)

```
[                    =========                     ]    610.9-891.6   ms  endpoint.flush
[                             =                    ]    891.7-901.7   ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                             ======               ]    901.8-1099.5  ms  tts.connect
```

### Turn 9 (repetition 0, fixture turn 9, e2e=2102.0ms)

```
[                       =======                    ]    939.2-1246.9  ms  endpoint.flush
[                              =                   ]   1247.0-1258.3  ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                               =========          ]   1258.3-1629.8  ms  tts.connect
```

### Turn 10 (repetition 0, fixture turn 10, e2e=1859.4ms)

```
[                        ======                    ]    875.6-1117.0  ms  endpoint.flush
[                              =                   ]   1117.0-1127.4  ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                              =========           ]   1127.4-1441.1  ms  tts.connect
```

### Turn 11 (repetition 0, fixture turn 11, e2e=1872.4ms)

```
[                    ========                      ]    747.6-1020.1  ms  endpoint.flush
[                            =                     ]   1020.1-1031.8  ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                            ===========           ]   1031.9-1436.5  ms  tts.connect
```

### Turn 12 (repetition 1, fixture turn 0, e2e=2076.5ms)

```
[                   =============                  ]    803.9-1320.5  ms  endpoint.flush
[                                =                 ]   1320.5-1328.8  ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                                ========          ]   1328.9-1673.5  ms  tts.connect
```

### Turn 13 (repetition 1, fixture turn 1, e2e=1740.9ms)

```
[                     ============                 ]    716.5-1129.3  ms  endpoint.flush
[                                 =                ]   1129.4-1137.6  ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                                 ======           ]   1137.7-1323.1  ms  tts.connect
```

### Turn 14 (repetition 1, fixture turn 2, e2e=1602.0ms)

```
[                    ===========                   ]    630.0-989.5   ms  endpoint.flush
[                               =                  ]    989.5-998.6   ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                               =========          ]    998.6-1271.9  ms  tts.connect
```

### Turn 15 (repetition 1, fixture turn 3, e2e=1881.2ms)

```
[                  ==========                      ]    658.5-1044.1  ms  endpoint.flush
[                            =                     ]   1044.2-1053.7  ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                            =========             ]   1053.7-1400.3  ms  tts.connect
```

### Turn 16 (repetition 1, fixture turn 4, e2e=1866.7ms)

```
[                       ==========                 ]    832.5-1206.6  ms  endpoint.flush
[                                 =                ]   1206.6-1216.1  ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                                 =====            ]   1216.1-1377.7  ms  tts.connect
```

### Turn 17 (repetition 1, fixture turn 5, e2e=1930.0ms)

```
[                 ==========                       ]    653.7-1027.2  ms  endpoint.flush
[                           =                      ]   1027.2-1037.8  ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                           ========               ]   1037.8-1313.5  ms  tts.connect
```

### Turn 18 (repetition 1, fixture turn 6, e2e=2112.8ms)

```
[                      ========                    ]    942.7-1266.1  ms  endpoint.flush
[                              =                   ]   1266.2-1274.8  ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                              =========           ]   1274.8-1633.4  ms  tts.connect
```

### Turn 19 (repetition 1, fixture turn 7, e2e=1422.5ms)

```
[                 ============                     ]    463.4-804.1   ms  endpoint.flush
[                             =                    ]    804.1-813.7   ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                             =======              ]    813.8-986.3   ms  tts.connect
```

### Turn 20 (repetition 1, fixture turn 8, e2e=1854.9ms)

```
[                    ===========                   ]    735.2-1130.0  ms  endpoint.flush
[                               =                  ]   1130.0-1139.9  ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                               =====              ]   1140.0-1309.6  ms  tts.connect
```

### Turn 21 (repetition 1, fixture turn 9, e2e=1839.7ms)

```
[                    ============                  ]    728.1-1147.4  ms  endpoint.flush
[                                =                 ]   1147.4-1157.8  ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                                =====             ]   1157.9-1320.3  ms  tts.connect
```

### Turn 22 (repetition 1, fixture turn 10, e2e=1873.3ms)

```
[                ============                      ]    598.6-1040.7  ms  endpoint.flush
[                            =                     ]   1040.7-1053.3  ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                            =============         ]   1053.3-1508.5  ms  tts.connect
```

### Turn 23 (repetition 1, fixture turn 11, e2e=1769.3ms)

```
[                   ============                   ]    667.8-1086.2  ms  endpoint.flush
[                               =                  ]   1086.2-1098.7  ms  llm.push (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)
[                               =====              ]   1098.7-1283.2  ms  tts.connect
```

## detection_lag / server_internal / network split (ms)

Every row below is over the 24 measured turn(s) of 24 attempted; the 0 unmeasurable turn(s) are in none of them (see *Turn measurement coverage*).

0 of those 24 measured turn(s) had no detection-lag measurement (no `endpoint.vad_eot` anchor found) and are excluded from the `detection_lag` row below as well — a percentile computed over fewer samples than the reader assumes would itself be misleading.

LLM stage note: this run's LLM is (measured-local — a co-located vLLM has ~zero network cost, unlike a production remote LLM)

| stage | n | p50 | p90 | p99 |
| --- | --- | --- | --- | --- |
| detection_lag | n=24 | 629.96 | 798.78 | 938.98 |
| server_internal | n=24 | 1798.45 | 2060.86 | 2112.32 |
| network | n=24 | 39.41 | 40.97 | 41.21 |

### network residual plausibility

All 24 measured turn(s) pass `residual_is_plausible` against the measured p50 RTT of 0.04ms.

## Turn-taking quality

These get worse as endpointing is tuned more aggressively — read them alongside the latency numbers above, not instead of them.

- premature_cut_rate: 0.0% (agent's first audio arrived before the user actually finished speaking)
- missed_endpoint_rate: 0.0% (agent never answered the turn at all)

| stage | n | p50 | p90 | p99 |
| --- | --- | --- | --- | --- |
| endpoint_delay_ms | n=24 | 629.96 | 798.78 | 938.98 |
