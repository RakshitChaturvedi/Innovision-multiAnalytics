#!/usr/bin/env bash
# make demo VIDEO=path/to/demo.mp4 [CAMERA_ID=<uuid>] [FPS=10]
# Starts the alert sink, detection, recognition, event_processing and the
# feeder (looping). Ctrl-C stops everything and prints the sink summary.
# Needs `make infra` + `make migrate` and the model weights (YOLOV11M_PATH,
# MODEL_ROOT); no video or weights ship with the repo.
set -euo pipefail
: "${VIDEO:?set VIDEO=path/to/video}"
CAMERA_ID="${CAMERA_ID:-$(python -c 'import uuid; print(uuid.uuid4())')}"
FPS="${FPS:-10}"
export DETECTION_CAMERA_IDS="$CAMERA_ID"
echo "demo camera_id=$CAMERA_ID"

pids=()
cleanup() {
  trap - INT TERM EXIT
  # sink last, so it sees the final alerts and prints its summary
  for p in "${pids[@]:1}"; do kill -TERM "$p" 2>/dev/null || true; done
  sleep 2
  kill -INT "${pids[0]}" 2>/dev/null || true
  wait || true
}
trap cleanup INT TERM EXIT

python -m tools.mock_platform.alert_sink --camera-id "$CAMERA_ID" & pids+=($!)
python -m services.detection.main & pids+=($!)
python -m services.recognition.main & pids+=($!)
python -m services.event_processing.main & pids+=($!)
python -m tools.mock_platform.feeder --video "$VIDEO" --camera-id "$CAMERA_ID" \
  --fps "$FPS" --loop & pids+=($!)
wait -n
