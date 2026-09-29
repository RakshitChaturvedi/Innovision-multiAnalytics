# Demo tuning

Settings that decide how the pipeline *feels* in a live demo: how fast a
headcount breach opens and resolves, and what a CPU-only box can keep up with.
Every variable below can go in `.env` (environment variables win over `.env`).

## Headcount: when does a breach open and resolve?

Per zone, headcount keeps a **rolling window** of per-frame counts over the
last `HEADCOUNT_ROLLING_WINDOW_SECONDS` of *event time* and uses their plain
average (`services/event_processing/src/workers/headcount/window.py`). Detection
publishes an event for every frame, including frames with nobody in them, so
an emptied zone does feed zeros into the window.

The breach state machine (`policies/headcount_policy.py`) has hysteresis:

| Transition | Condition | Must hold for |
|---|---|---|
| NORMAL → BREACH (one alert) | `rolling_avg > max_headcount` | `HEADCOUNT_BREACH_ENTER_SECONDS` |
| BREACH → NORMAL (resolved) | `rolling_avg <= max_headcount - margin` | `HEADCOUNT_BREACH_EXIT_SECONDS` |

`margin = max(1, ceil(0.1 * max_headcount))`, e.g. 1 for limits up to 10, 2 for
11–20. Hovering at the limit therefore never flaps; a breach resolves only once
the zone is clearly back under the limit.

### Timing

With window `W`, a count that jumps from `before` to `after` and a target
average `target`, the average reaches the target after about

```
t = W * |target - before| / |after - before|        (t <= W)
```

so

* **open**:    `W * (max - before) / (after - before)` + `ENTER` seconds
* **resolve**: `W * (after_breach - (max - margin)) / (after_breach - after)` + `EXIT` seconds

Example, `max_headcount = 5` (margin 1, resolve below 4.0), 6 people walk in
and later all leave:

| Setting | Breach opens after | Breach resolves after |
|---|---|---|
| defaults: `W=30`, `ENTER=5`, `EXIT=10` | 30·5/6 + 5 ≈ **30 s** | 30·2/6 + 10 ≈ **20 s** |
| demo: `W=10`, `ENTER=5`, `EXIT=5` | 10·5/6 + 5 ≈ **13 s** | 10·2/6 + 5 ≈ **8 s** |

If only some people leave, it takes longer: 6 → 4 people needs the whole window
to wash out (`W·2/2 = W`) plus `EXIT`. If the count stays above
`max - margin` (6 → 5 with a limit of 5), the breach does **not** resolve.
That is the hysteresis working as intended.

### Camera goes silent

If a camera stops sending detection events, its open breaches are resolved
with reason `camera_stale` after `HEADCOUNT_STALE_CAMERA_S` of *wall clock*
without events. The sweeper runs every `HEADCOUNT_SWEEP_INTERVAL_S`, so the
worst case is `STALE_CAMERA_S + SWEEP_INTERVAL_S` (default 30 + 5 s).

### Recommended demo values

```env
HEADCOUNT_ROLLING_WINDOW_SECONDS=10
HEADCOUNT_BREACH_ENTER_SECONDS=5
HEADCOUNT_BREACH_EXIT_SECONDS=5
HEADCOUNT_STALE_CAMERA_S=15
```

Keep `ENTER` and `EXIT` at a few seconds or more. At 0 s, a person
briefly occluded or double-detected at the limit can open or resolve a breach.

## Zones and intruder

* `LOST_TRACK_TIMEOUT_S=2.0`: a track missing from the events for this long
  (event time) produces EXITED. This needs events to keep flowing; a camera
  that goes silent is swept after `STALE_CAMERA_S`.
* Intruder alerts need recognition results. The alert decision uses every
  recognition of the track in `[zone_ts - RECOGNITION_MAX_AGE_S,
  zone_ts + RECOGNITION_FUTURE_S]`: an authorized person must hold a strict
  majority of the track's rows. The same enrolled person matched on two tracks
  of one camera in that window fails closed (`unidentified_in_restricted`).
  If ByteTrack loses a person and re-acquires them under a new track id within
  that window, the same person appears on two tracks, so keep tracks stable
  (see `TRACK_BUFFER` below).
* Face → track assignment (`RECOGNITION_HEAD_WIDTH_FRAC=0.7`,
  `RECOGNITION_HEAD_TOP_MARGIN_FRAC=0.05`, `RECOGNITION_HEAD_HEIGHT_FRAC=0.30`,
  `RECOGNITION_FACE_AMBIGUITY_RATIO=1.25`): a face is used only by the track
  whose head region contains it and whose box top is nearest. Widen the head
  region only if tracks with visible faces record nothing
  (`face_no_face_in_head_region` in the recognition stats).

## CPU-only boxes

YOLOv11m and InsightFace on CPU cannot keep up with a full-rate video. When
the pipeline falls behind, frames expire from Redis (TTL 20 s) and are
dropped (`frame_expired` in the stats). Recommended:

```env
# feed the demo video at 3-5 fps (mock feeder / platform ingestion setting)
DETECTION_IMGSZ=416        # instead of 640: ~2.3x fewer pixels per frame
USE_GPU=false
TRACKER_FRAME_RATE=5       # match the feed rate
TRACK_BUFFER=60            # ByteTrack keeps a lost track TRACK_BUFFER/30 s (here 2 s)
BATCH_SIZE=4
BATCH_TIMEOUT_MS=100
DEFAULT_SAMPLE_RATE=5      # recognition every 5th frame per track (~1 s at 5 fps)
```

* `DETECTION_IMGSZ=416` costs some recall on small or far-away people. Stay at
  640 if people in the demo video are small.
* Recognition only fetches and decodes a frame when at least one track is due
  for sampling. A new track is always due, then every `DEFAULT_SAMPLE_RATE`-th
  frame, or earlier when detection confidence improves by
  `QUALITY_IMPROVEMENT_THRESHOLD`.
* Watch the periodic `<consumer> stats {...}` line (INFO). Per-frame
  `frame_processed` logs are DEBUG (`LOG_LEVEL=DEBUG` for detection).

## MinIO

Frames are read from Redis first and from MinIO (`innovision-frames`) on a
miss. With `MINIO_ENDPOINT=` (empty), or while MinIO is unreachable, a Redis
miss is `FrameUnavailable`: the message is acked and counted as
`frame_expired`, and the outage is logged once. `MINIO_TIMEOUT_S` (default 3)
bounds how long an unreachable MinIO can stall a frame.
