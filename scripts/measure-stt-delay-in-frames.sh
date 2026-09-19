#!/usr/bin/env bash
# Measure the one latency lever left unquantified: the Gradium STT's
# `delay_in_frames` lookahead, which gradbot never sets.
#
# WHY IT MATTERS. One STT frame is 80ms (sample_rate=24000, frame_size=1920,
# measured live). The documented range is 0-80, i.e. 0-6.4s of lookahead, and
# our measured detection lag of ~610ms is about 7.9 frames. If the streaming
# default is ~8 frames, the largest block in the latency budget is one unset
# parameter. Confirmed accepted by the API; never quantified, because prod STT
# sat at its shared 20-session cap.
#
# RUN IT WHEN THE KEY IS QUIET. Each point opens N sessions against a shared
# 20-session cap; hitting the cap degrades service for everyone on that key.
# If you see "Concurrency limit exceeded", stop and come back later rather than
# retrying — retries make it worse.
#
# Usage:  scripts/measure-stt-delay-in-frames.sh [reps] [frames...]
#   e.g.  scripts/measure-stt-delay-in-frames.sh 3 unset 8 4 2
set -euo pipefail
cd "$(dirname "$0")/.."
REPS="${1:-3}"; shift || true
POINTS=("${@:-unset 8 4 2}")
OUT="${OUT_DIR:-/tmp/dif-$(date +%s)}"; mkdir -p "$OUT"

command -v cargo >/dev/null || export PATH="$HOME/miniforge3/envs/binaries/bin:$PATH"
[ -f ~/.venv ] && { set -a; . ~/.venv; set +a; }
: "${GRADIUM_API_KEY:?source ~/.venv first}"
: "${LLM_BASE_URL:?export LLM_BASE_URL=http://<host>:<port>/v1  (note the /v1)}"

for D in "${POINTS[@]}"; do
  pkill -x gradbot_bin 2>/dev/null || true; sleep 20
  mkdir -p "$OUT/t_$D"
  sed "s#^trace_dir = .*#trace_dir = \"$OUT/t_$D\"#" configs/gradbot.toml > "$OUT/c_$D.toml"
  setsid nohup ./target/release/gradbot_bin --config "$OUT/c_$D.toml" > "$OUT/s_$D.log" 2>&1 &
  sleep 8
  EXTRA=(); [ "$D" != unset ] && EXTRA=(--stt-extra-config "{\"delay_in_frames\": $D}")
  ./target/release/examples/gradbot-bench \
    --url ws://127.0.0.1:8123/v1/realtime \
    --manifest docs/superpowers/fixtures/turn_taking.json \
    --repetitions "$REPS" "${EXTRA[@]}" \
    --out-marks "$OUT/m_$D.json" --trace-dir "$OUT/t_$D" --out-report "$OUT/r_$D.md" \
    > "$OUT/run_$D.log" 2>&1 || true
  echo "=== delay_in_frames=$D ==="
  if grep -q 1008 "$OUT/s_$D.log" 2>/dev/null; then
    echo "  ABORT: hit the shared STT session cap. Stop and retry when the key is quiet."
    pkill -x gradbot_bin 2>/dev/null || true; exit 1
  fi
  # latency AND the two quality guards -- less lookahead may simply transcribe worse
  grep -E "^\| detection_lag|premature_cut_rate|missed_endpoint" "$OUT/r_$D.md" || true
  python3 - "$OUT/t_$D" <<'PY'
import json,glob,sys
c=s=0
for p in glob.glob(sys.argv[1]+"/*.jsonl"):
    for l in open(p):
        if not l.strip(): continue
        r=json.loads(l)
        if r["span"]=="stt.text": s+=1; c+=r.get("attrs",{}).get("chars",0)
print(f"  transcript: {s} spans, {c} chars  <-- must NOT drop vs baseline (same audio in)")
PY
done
pkill -x gradbot_bin 2>/dev/null || true
echo "results in $OUT"
