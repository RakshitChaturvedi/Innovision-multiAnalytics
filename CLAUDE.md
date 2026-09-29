# CLAUDE.md — Innovision multiAnalytics (use case layer)

Read this whole file before every task. It is the source of truth for how this repo must behave.

## 1. What this repo is

Video analytics use case (restricted zones, intruder detection, headcount) that runs ON TOP of the
Innovision platform. The platform owns cameras, ingestion, alerts, incidents, dashboard.
This repo only consumes frames and produces alerts.

```
PLATFORM                              THIS REPO                                   PLATFORM
frames:{camera_id}  ──▶ detection (YOLOv11m + ByteTrack) ──▶ events:detections
(Redis stream,           │                                       │
 FrameEvent JSON)        │                          ┌────────────┼──────────────┐
                         ▼                          ▼            ▼              ▼
frame:{cam}:{seq}   recognition (InsightFace)   zone_monitor  headcount   (recognition
(Redis, TTL 20s)         │ ─▶ recognition_events    │ ─▶ events:zone        also reads
                         │    (Postgres)            ▼                        events:detections)
innovision-frames        │                      intruder ──────────────────▶ alerts:live ──▶ Alert Management
frames/{cam}/{seq:08d}.jpg (MinIO)              headcount ─────────────────▶ alerts:live
```

Models are already trained. NEVER change model weights, training code, or model paths logic
beyond reading them from env (`YOLOV11M_PATH`, `MODEL_ROOT`, `RECOGNITION_MODEL_PACK`).

## 2. Platform contracts (NON-NEGOTIABLE)

The platform's contracts are vendored UNCHANGED in `shared/platform_contracts/`
(`enums.py`, `frame_event.py`, `alert_event.py`). Never edit those files. Never redefine them.

- Input `FrameEvent` (platform): `event_id, camera_id, frame_seq, timestamp, frame_provider ("redis"|"minio"),
  frame_reference, frame_shape (h, w)`. There is NO `profile` field.
- `frame_reference` is OPAQUE. For provider `redis` it is the exact Redis key (currently `frame:{cam}:{seq}`).
  `GET` it verbatim. Never add or strip prefixes.
- Cold copy of every frame: MinIO bucket `innovision-frames`, key `frames/{camera_id}/{frame_seq:08d}.jpg`.
  Build this key ONLY via `shared/frames.py::minio_frame_key()`.
- Output `AlertEvent` (platform): requires `source_event_id` (single UUID), `source_uc` (`uc1..uc4`),
  non-empty `title` (<=200) and `description` (<=2000), `alert_type` (string).
  `frame_reference` and `frame_provider` must BOTH be set or BOTH be None.
- Alerts are published ONLY through `shared/alerting/publisher.py`, which validates against the platform
  model and the platform `AlertEventValidator` rules before `XADD alerts:live`.
- Platform Alert Management dedupes on `alert_id`, so alert ids we publish must be DETERMINISTIC
  (uuid5 of the domain event id) so retries never create duplicate alerts.

Internal events (`DetectionEvent`, `RecognitionEvent`, `ZoneEvent`) are owned by this repo and live in
`shared/schemas/events.py`. They must use the PLATFORM `FrameProvider` enum.

## 3. Engineering rules

1. **No silent failures.** Every failure path must end in exactly one of:
   - success → XACK
   - transient error → raise, message stays pending, reclaimed by XAUTOCLAIM and retried
   - permanent error (bad payload, frame expired) → raise `PermanentError` → copied to `{stream}:dlq` with
     the error, then XACK
   Never `except Exception: pass`. Never `return` early from an error without logging at WARNING or above
   AND counting it in the consumer's metrics.
2. **Ordering.** Messages for the same `camera_id` are processed strictly in order. Different cameras
   run concurrently. This is enforced in `BaseStreamConsumer`; do not bypass it with `asyncio.gather`.
3. **Time.** Business logic uses the EVENT timestamp (`frame timestamp` / `detection_event.timestamp`),
   never `datetime.now()`. Wall clock is only for sweepers that detect stale cameras.
4. **Idempotency.** Every DB insert that can be retried uses a deterministic id or a unique constraint with
   `ON CONFLICT DO NOTHING`. Every publish after a DB write must be safe to repeat.
5. **Separate database.** This repo uses database `innovision_analytics` and Alembic version table
   `alembic_version_analytics`. Never touch the platform database `innovision_platform`.
6. **jsonb / vector with asyncpg:** pass `json.dumps(...)` + `CAST(:x AS jsonb)`; vectors as `'[..]'` text
   + `CAST(:x AS vector)`; read vectors with pgvector's asyncpg codec or parse the text.
7. **Blocking calls** (MinIO SDK, model inference, cv2 on big frames) go through `asyncio.to_thread`.
8. **Config** comes from env with safe defaults. A service must not require unrelated secrets
   (Grafana, JWT) to start. Settings use `extra="ignore"`.
9. **Background tasks** (pub/sub listeners, sweepers) run under a supervisor that logs and restarts them;
   a dead listener must never be silent.
10. Every bug fix ships with a test that FAILS before the fix and PASSES after it.
11. Small, focused commits: `fix(<service>): <what>`. Show the diff and test output before committing.

## 4. Local commands

```bash
make infra        # docker compose --env-file .env -f infra/docker-compose.dev.yml up -d  (redis, postgres+pgvector, minio, bucket init; creates .env from .env.example if missing)
make migrate      # alembic -c migrations/alembic.ini upgrade head   (DATABASE_URL -> innovision_analytics)
make test         # pytest -q
make health       # python tools/check_health.py  (stream lengths, pending, DLQ, lag)
```

Dev Redis runs with `--maxmemory-policy volatile-lru --appendonly yes` (same as what we ask the platform to use).

### Test isolation (NON-NEGOTIABLE)

- Tests must NEVER point at the platform's Redis. They use a dedicated database index
  `TEST_REDIS_DB` (default 15) on `TEST_REDIS_URL` (host/port, default `redis://127.0.0.1:6379`).
  A session guard in `tests/conftest.py` refuses to run if the target database is 0.
- Only that database is flushed (`flushdb`, never `flushall`). Build every test Redis URL with
  `tests/redis_target.py` and point services under test at it with `point_services_at_test_redis()`
  (services read `REDIS_DB` from env, default 0).
- Postgres tests use `TEST_DATABASE_URL` (recreated per run; tests that drop the schema use its
  `_scratch` sibling). Real-service fixtures live in `tests/conftest.py`; they skip locally when a
  service is unreachable and FAIL in CI (`REQUIRE_REAL_SERVICES=1`).

## 5. Streams and groups

| Stream | Producer | Consumer groups |
|---|---|---|
| `frames:{camera_id}` | platform ingestion | `detection_group` |
| `events:detections` | detection | `zone_monitor_group`, `headcount_group`, `recognition_group` |
| `events:zone` | zone_monitor | `intruder_group` |
| `events:recognitions` | recognition | (none yet) |
| `alerts:live` | intruder, headcount | platform `alert_management_group` |
| `{stream}:dlq` | BaseStreamConsumer | humans / `tools/check_health.py` |

## 6. Known bugs this branch fixes (checklist)

- [ ] Contracts: FrameEvent `profile` rejects every platform frame; frame key gets `frames:` prefix; AlertEvent missing `source_event_id`/`source_uc`, provider/ref mismatch.
- [ ] Recognition reads frames from wrong MinIO bucket/key, blocking call.
- [ ] Alembic `0004_intruder` down_revision `"0003_recognition"` does not exist.
- [ ] Dockerfiles/import paths broken (event_processing, recognition); compose build contexts.
- [ ] `shared/config.py` requires unrelated secrets; `RedisFrameCache` typo `decode_response`; `StorageClient` indexes a list by string.
- [ ] BaseStreamConsumer: no pending reclaim, no DLQ, `gather` breaks per-camera order, swallows group-create errors, dead backpressure code.
- [ ] Detection: single hardcoded camera; out-of-order frames into ByteTrack; confidence re-filter causes track flicker; track ids reused after restart.
- [ ] Recognition: negative similarity violates CHECK/pydantic; AuditWriter dict→jsonb fails; pgvector read as string; pose index order (InsightFace pose = [pitch, yaw, roll]); frame_seq going backwards breaks sampler.
- [ ] Zone monitor: no EXITED for lost tracks; DWELL every frame; wall clock; zone cache never expires; listener dies silently.
- [ ] Intruder: fail-open when no recognition; unbounded recognition lookup; DB insert + XADD not atomic; alert_type mapping.
- [ ] Headcount: breach on instantaneous count (flapping); DB query per frame; duplicate breaches; breach never resolves when camera goes stale.

### Runtime setup (real platform only)

The use case runs as Python processes (detection, recognition, event_processing) on the host, against
the REAL platform stack on localhost: platform Redis (`frames:*`, `frame:*`, `alerts:live`), Postgres
(this repo's database `innovision_analytics` only), MinIO (`innovision-frames`) and the platform camera
registry.
The platform's `docker-compose.stubs.yml` must NEVER run alongside: its stub services would produce
frames/consume alerts in place of the real platform. `infra/docker-compose.dev.yml` is for tests only.

## 7. Definition of done (acceptance)

Run against the real platform with real cameras for 30 minutes; acceptance is `tools/check_integration.py`:
1. `make test` green.
2. Headcount: count per zone matches reality; exactly ONE alert per breach episode; no flapping at the limit; breach resolves after people leave or camera goes stale.
3. Intruder: unknown person in restricted zone ALWAYS alerts (also with face turned away); authorized enrolled person NEVER alerts; one alert per intrusion.
4. Zones: leaving the frame produces EXITED within ~2 s; DWELL fires once per track/zone.
5. Every alert passes the platform `AlertEvent` + `AlertEventValidator` (`tools/check_integration.py` reports 0 invalid).
6. Chaos: kill -9 each service mid-run and restart → no lost and no duplicate alerts. Restart Redis/Postgres for 30 s → processing resumes by itself.
7. `make health` after the run: 0 pending older than 60 s in every group, DLQ entries only for deliberately injected bad messages.
