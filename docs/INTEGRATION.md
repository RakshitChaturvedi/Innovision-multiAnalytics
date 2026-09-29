# Running the use case against the real Innovision platform

This runbook wires this repo (detection, recognition, event_processing) to the REAL platform
stack running on the same Windows machine: platform Redis, Postgres, MinIO, camera registry,
auth, ingestion and Alert Management on `localhost`. The use case runs as three Python
processes from this repo's venv; it does not run in Docker.

**Never start the platform's `infra\docker-compose.stubs.yml`.** Its stub services produce
frames and consume alerts in place of the real platform. Every script below FAILs if a stub
container is running.

Conventions:

* Run everything from PowerShell in this repo's folder (`cd C:\innovision\Innovision-multiAnalytics`).
* `<PlatformPath>` is the platform repo folder (the one with `infra\docker-compose.yml`), for example
  `C:\innovision\platform`. `<compose>` below means `<PlatformPath>\infra\docker-compose.yml`.
* Every step is one command. Every check prints `PASS`, `WARN` or `FAIL` lines. A `FAIL` line is always
  followed by a `FIX:` line, and the command ends with `RESULT: PASS` or `RESULT: FAIL`. Fix every FAIL
  before you go to the next step.
* Nothing prints secrets. URLs are shown as `rtsp://***@10.0.0.9/...`.
* If a step fails and the FIX does not help, run step 13 and send the zip.

## 0. One-time setup

1. Create the venv with Python 3.12 and install the dependencies:
   `py -3.12 -m venv .venv; .venv\Scripts\python -m pip install -e .` and the service requirements
   (`services\detection\requirements.txt`, `services\recognition\requirements.txt`,
   `services\event_processing\requirements.txt`).
2. Copy the env template: `Copy-Item .env.platform.example .env`, then fill in every `<...>` value.
   `INTERNAL_SERVICE_TOKEN`, `MINIO_ACCESS_KEY` and `MINIO_SECRET_KEY` must equal the platform `.env`.
   Keep `REDIS_DB=0` (the platform's Redis). Tests never use database 0.

## 1. Prerequisites

```powershell
.\scripts\check_prereqs.ps1 -PlatformPath <PlatformPath>
```

This checks:

* Docker Desktop is running.
* Python 3.12 and the venv packages are present.
* `ffmpeg` and `ffprobe` are on PATH.
* The git branch of both repos.
* Both `.env` files exist, and the service token and MinIO keys are equal in both.
* The YOLO weights file exists, and `MODEL_ROOT\models\<pack>\*.onnx` exists.
* Redis, Postgres, MinIO, the registry and auth are reachable.
* No stub container is running.

PASS looks like `RESULT: PASS (0 warnings)`. The first time, Redis, Postgres, MinIO, the registry and
auth FAIL because the platform stack is not running yet. Start it in step 2 and run this again.

## 2. Start the platform stack (never the stubs)

```powershell
docker compose -f <PlatformPath>\infra\docker-compose.yml up -d
```

PASS: run step 1 again and it ends with `RESULT: PASS`. If it FAILs on `stubs`, run the command in its
FIX (`docker compose -f <PlatformPath>\infra\docker-compose.stubs.yml down`).

## 3. Test each camera URL

```powershell
.\scripts\test_camera_url.ps1 -Url http://192.168.1.20:4747/video
```

This grabs ONE frame with ffmpeg and saves it as `camera_test_<time>.jpg`. It prints the resolution and
the fps (if the stream reports it). It works for DroidCam (HTTP MJPEG) and RTSP.

* **PASS:** `PASS frame: ... (1280x720)` and `PASS aspect: 1280x720 is 16:9`.
* **WARN aspect:** the platform stretches every source to 1920x1080. Set the phone or camera to 16:9
  (1280x720 or 1920x1080) and test again.
* **FAIL:** see the "DroidCam" section below.

## 4. Register the cameras

Run once per camera:

```powershell
.\scripts\register_cameras.ps1 -Name "Phone 1" -Location "Lobby" -Url http://192.168.1.20:4747/video -Fps 5 -PlatformPath <PlatformPath>
```

It prompts for the platform admin email and password. They are never stored.

* **New name:** creates the camera with `use_cases ["uc1"]`, then checks that `/cameras/by-uc/uc1` lists it.
* **Name already registered:** prints its id and whether the URL, fps or use_cases differ. It changes
  nothing unless you add `-Update`. With `-Update` it sets the new URL and fps and adds `uc1` to
  `use_cases`, keeping the other use cases. Deleting cameras is not supported here: use the platform UI.

PASS: `PASS camera: 'Phone 1' created, id ...` and `PASS discovery: ... is listed by /cameras/by-uc/uc1`.
Keep `-Fps` equal to `TRACKER_FRAME_RATE` in `.env` (5 on CPU).

## 5. Restart Alert Management

Alert Management loads the camera list only when it starts. Step 4 prints the exact command, for example:

```powershell
docker compose -f <PlatformPath>\infra\docker-compose.yml restart alert_management
```

PASS: `docker compose -f <compose> ps` shows the service `running`. Until you restart it, alerts for new
cameras fail the platform's validation and end up in `alerts:dead_letter`.

## 6. Create and migrate this repo's database

```powershell
.\scripts\platform_db.ps1
```

This creates `innovision_analytics` on the platform Postgres (if it is missing), runs
`CREATE EXTENSION vector` and `alembic upgrade head`, then prints the alembic heads. It refuses to run if
`DATABASE_URL` names any other database, and it never touches `innovision_platform`.

* **PASS:** `PASS alembic: heads: 0010_... (head); current: 0010_... (head)`.
* **FAIL pgvector:** the FIX says whether the extension is not installed (the platform Postgres image
  needs pgvector) or not permitted (run the printed SQL as the Postgres superuser).

## 7. Start the platform ingestion so frames flow

Ingestion reads the registered cameras when it starts, so restart it after step 4. Use the ingestion
service name from `<compose>` (`docker compose -f <compose> config --services` lists them):

```powershell
docker compose -f <PlatformPath>\infra\docker-compose.yml restart ingestion
```

PASS: step 8 finds a frame for every camera.

## 8. Snapshot each camera

```powershell
.venv\Scripts\python -m tools.config.snapshot --camera "Phone 1"
```

This saves `snapshot_Phone_1_<time>.png`: the camera's REAL current frame, as the platform delivers it,
with a 10x10 grid labelled 0.0-1.0. Zone corners are `[x, y]` read off this grid: x to the right, y
downwards, `[0.0, 0.0]` top-left, `[1.0, 1.0]` bottom-right.

* **PASS:** `PASS snapshot: snapshot_Phone_1_....png (1920x1080, frame seq ..., 0s old)`.
* **FAIL frame:** no frames for this camera. Check step 7 and the camera (step 3).

## 9. Write config.yaml

```powershell
Copy-Item tools\config\config.example.yaml config.yaml
```

Edit `config.yaml`:

* **Cameras:** the exact names from step 4. Each camera's zones have:
  * `type`: `monitored` for headcount, `restricted` for intruder alerts, or `safe`
  * `polygon`: at least 3 points from the snapshot grid
  * `max_headcount`: for monitored zones
  * `dwell_threshold_seconds`
* **People:**
  * `images_dir`: a folder of photos, each with exactly one sharp, frontal, well-lit face. Use forward
    slashes: `C:/innovision/enrollment/alice`.
  * `authorized_zones`: entries of the form `"camera name/zone name"`.
  * `blocklisted`: `true` or `false`.

Photos stay on this machine. Never commit them or `config.yaml`.

## 10. Apply the config: dry run, then for real

```powershell
.venv\Scripts\python -m tools.config.apply_config config.yaml --dry-run
.venv\Scripts\python -m tools.config.apply_config config.yaml
```

The dry run prints the full plan and writes nothing. The plan uses these verbs:

* `CREATE`, `UPDATE`, `UNCHANGED` for zones and people
* `ENROLL` for each new photo
* `AUTHORIZE` and `REMOVE` for authorizations
* `BLOCKLIST` and `UNBLOCKLIST`
* `KEEP` for zones of a listed camera that are not in the file

Read every `REMOVE` line. For the people in the file, the file is the source of truth. People who are not
in the file are not touched.

* **Real run:** it writes everything in one transaction, then tells the running services to reload zones
  and enrollments.
* **Photos:** the first run with photos loads the InsightFace model, so it takes noticeably longer
  (up to a minute on CPU). Photos without exactly one good face are skipped and the reason is printed,
  for example `2 faces in the photo` or `too_blurry:9.8`. A skipped photo is not retried unless you pass
  `--retry-skipped`.
* **Re-running** with the same file changes nothing: every line is `UNCHANGED`.
* **`--prune`** deletes zones of the listed cameras that are not in the file, and their authorizations.
  It never deletes a zone with an open intruder event or an open headcount breach; those zones are
  reported and kept. Run `--dry-run --prune` first.

PASS: `RESULT: PASS`. On any FAIL (unknown camera name, bad polygon, unknown `authorized_zones` entry)
nothing is written.

## 11. Start the use case

```powershell
.\scripts\up.ps1 -PlatformPath <PlatformPath>
```

It runs a preflight first:

* Redis and MinIO are reachable.
* The service token is set and accepted.
* No stub container is running.
* `/cameras/by-uc/uc1` lists at least one camera. The cameras are printed.

Then it starts detection, recognition and event_processing in the background. Logs go to
`logs\<service>.log` and PID files to `logs\<service>.pid`.

PASS: `PASS detection: pid ..., /health on :8081 ok`, and the same for recognition (8082) and
event_processing (8083). If a service exits during startup, the FAIL line shows the last log lines.

To stop: `.\scripts\down.ps1`. It stops the services gracefully and force-kills them after 20 s.
Force-killing is safe: unacknowledged messages are delivered again.

## 12. Check the integration (acceptance)

```powershell
.venv\Scripts\python -m tools.check_integration
```

This is read-only: it creates no consumer group and only runs SELECTs.

**For each camera:**

* Frames in the last 10 s. FAIL if there are none.
* Detection events in the last 60 s. WARN if there are none.
* Recognition rows in the last 60 s. WARN if there are none, with recognition's top rejection reason
  (see `docs/DEMO_TUNING.md`).

**Services and streams:**

* `/health` of the three services.
* Pending age per stream and group. FAIL if our groups have messages pending longer than 60 s.
* Every `*:dlq` stream. FAIL if one is not empty.

**Alerts:**

* Every `uc1` alert on `alerts:live` in the last 10 minutes (`--window` changes this) is validated with
  the platform `AlertEvent` and `AlertEventValidator`, and no `alert_id` may appear twice.
* Every published alert must be stored in the platform's `alerts` table (`PLATFORM_DATABASE_URL`, used
  read-only).

PASS: `RESULT: PASS`. Run it again during and after a test session.

## 13. If anything fails: collect diagnostics

```powershell
.\scripts\collect_diagnostics.ps1 -PlatformPath <PlatformPath>
```

This writes `diagnostics_<time>.zip`, which contains:

* the last 500 lines of each use case log
* the output of step 12
* the `/health` bodies
* stream, group, pending and DLQ status
* the alembic heads
* `docker compose ps`
* the last 300 lines of the platform's ingestion, camera registry and Alert Management logs
* both `.env` files

Every token, password, key and URL credential is masked. The zip contains no model files and no photos.
Send the zip.

## Known platform issues and workarounds

* **`INTERNAL_SERVICE_TOKEN` is missing from the platform.** Add it by hand to the platform `.env` AND to
  the `camera_registry` environment in `<compose>`, then
  `docker compose -f <compose> up -d camera_registry`. Copy the same value into this repo's `.env`.
  Step 1 checks that the two are equal.
* **Restart Alert Management after registering cameras** (step 5). It loads cameras only at startup.
  Until then, alerts for new cameras fail validation.
* **Set phones to 16:9.** The platform stretches every source to 1920x1080, so a 4:3 image distorts
  people and zones. Step 3 warns about this.
* **The platform camera status is unreliable** (a camera can show offline while frames flow). Use step
  12, which looks at the frames themselves.
* **Don't restart Redis in the middle of a test.** Streams, frames and pending messages live there. The
  services reconnect by themselves, but frames from the gap are lost and the run is not comparable.
* **If alerts stop reaching the platform,** restart Alert Management and inspect its dead letter stream:
  `docker compose -f <compose> exec redis redis-cli XRANGE alerts:dead_letter - + COUNT 5`.
  Use the Redis service name from `<compose>` if it is not `redis`. Step 12 reports alerts that were
  published but not stored.

## DroidCam

* The phone and this PC must be on the same Wi-Fi network (no guest network or client isolation).
* Use the URL shown in the DroidCam app, usually `http://<phone-ip>:4747/video`. Test it with step 3
  before you register it.
* Set the resolution to 16:9 (1280x720 or 1920x1080).
* Keep the app in the foreground with the screen on, and keep the phone charging. Android stops the
  stream when the screen locks or the battery saver starts.
* Only one client can read DroidCam at a time. Close the browser tab or VLC before starting ingestion.
