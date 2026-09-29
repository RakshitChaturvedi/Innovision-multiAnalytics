"""Acceptance check of the use case running against the REAL platform.

    python -m tools.check_integration [--window 600]

READ-ONLY everywhere: XRANGE/XINFO/XPENDING (no consumer group is created or
read with), SELECT only, the platform database in a READ ONLY transaction.

Per camera of GET /cameras/by-uc/<SOURCE_UC>:
  frames:{cam}         entries in the last 10 s                     (FAIL if 0)
  events:detections    entries for the camera in the last 60 s      (WARN if 0)
  recognition_events   rows for the camera in the last 60 s         (WARN if 0, with
                       recognition's top rejection reason from /health)
Services: GET /health of detection (8081), recognition (8082), event_processing (8083).
Streams: oldest pending entry per group (FAIL > 60 s for our groups) and every
         {stream}:dlq (FAIL if not empty).
Alerts:  alerts:live entries of this use case in --window, each validated with the
         platform AlertEvent + AlertEventValidator (FAIL if invalid), no duplicate
         alert_id (FAIL), and every one stored by the platform
         (PLATFORM_DATABASE_URL, table alerts, compared on alert_id; FAIL if missing).
Exit 1 on any FAIL.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from uuid import UUID

from tools.platform.common import (
    HttpError, RegistryClient, RegistryError, Reporter, http_json, load_env, mask_text,
)

OUR_GROUPS = {"detection_group", "zone_monitor_group", "headcount_group", "recognition_group",
              "intruder_group"}
FIXED_STREAMS = ("events:detections", "events:zone", "events:recognitions", "alerts:live")
HEALTH = (("detection", 8081), ("recognition", 8082), ("event_processing", 8083))
FRAMES_WINDOW_S = 10
ACTIVITY_WINDOW_S = 60
MAX_PENDING_AGE_S = 60
SCAN_LIMIT = 50_000
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


def _s(v) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


def _ms(entry_id) -> int:
    return int(_s(entry_id).split("-")[0])


def _data(fields) -> str | None:
    raw = fields.get(b"data", fields.get("data"))
    return None if raw is None else _s(raw)


# ---------------------------------------------------------------- health


def fetch_health(ports=HEALTH, host="127.0.0.1") -> dict[str, tuple[int | None, dict | str]]:
    out = {}
    for name, port in ports:
        try:
            out[name] = http_json("GET", f"http://{host}:{port}/health", timeout=3)
        except HttpError as exc:
            out[name] = (None, str(exc))
    return out


def check_health(r: Reporter, health: dict) -> None:
    for name, (status, body) in health.items():
        if status == 200:
            since = body.get("seconds_since_last_message") if isinstance(body, dict) else None
            r.ok(f"health {name}", "ok" + (f", last message {since:.0f}s ago" if since is not None else ""))
        elif status is None:
            r.fail(f"health {name}", f"not reachable ({body})",
                   "start the services: scripts\\up.ps1 (logs in .\\logs)")
        else:
            detail = body if isinstance(body, str) else json.dumps(
                {k: body.get(k) for k in ("status", "redis", "db", "dead_tasks")}, default=str)
            r.fail(f"health {name}", f"HTTP {status}: {mask_text(detail)[:300]}",
                   f"read logs\\{name}.log; restart with scripts\\down.ps1 then scripts\\up.ps1")


def top_rejection(health: dict, camera_id: str) -> str | None:
    status, body = health.get("recognition", (None, None))
    if not isinstance(body, dict):
        return None
    counts: Counter = Counter()
    for c in body.get("consumers") or []:
        for reason, n in ((c.get("per_camera") or {}).get(camera_id) or {}).items():
            if reason != "rows_written":
                counts[reason] += int(n)
    if not counts:
        return None
    reason, n = counts.most_common(1)[0]
    return f"{reason} ({n}x since recognition started)"


# ---------------------------------------------------------------- streams


async def check_cameras(r: Reporter, redis, session_factory, cameras: dict[str, str], health: dict,
                        now_ms: int) -> None:
    det = Counter()
    entries = await redis.xrange("events:detections", min=str(now_ms - ACTIVITY_WINDOW_S * 1000),
                                 max="+", count=SCAN_LIMIT)
    for _id, fields in entries:
        try:
            det[str(json.loads(_data(fields) or "{}").get("camera_id"))] += 1
        except ValueError:
            continue
    rec = Counter()
    if session_factory is not None:
        from sqlalchemy import text

        async with session_factory() as s:
            rows = await s.execute(text(
                "SELECT camera_id, count(*) AS n FROM recognition_events "
                "WHERE timestamp > now() - make_interval(secs => :w) GROUP BY camera_id"),
                {"w": ACTIVITY_WINDOW_S})
            rec.update({str(row.camera_id): int(row.n) for row in rows})

    for cid, name in cameras.items():
        label = f"{name!r} {cid}"
        n = len(await redis.xrange(f"frames:{cid}", min=str(now_ms - FRAMES_WINDOW_S * 1000),
                                   max="+", count=SCAN_LIMIT))
        if n:
            r.ok(f"frames {name}", f"{n} in the last {FRAMES_WINDOW_S}s")
        else:
            r.fail(f"frames {name}", f"{label}: no frame in the last {FRAMES_WINDOW_S}s",
                   "start the platform ingestion for this camera; check the phone app is open "
                   "and the URL works (scripts\\test_camera_url.ps1)")
        if det[cid]:
            r.ok(f"detections {name}", f"{det[cid]} events in the last {ACTIVITY_WINDOW_S}s")
        else:
            r.warn(f"detections {name}", f"no detection event in the last {ACTIVITY_WINDOW_S}s",
                   "if frames flow: read logs\\detection.log (is the camera in by-uc? restart "
                   "detection after registering)")
        if session_factory is None:
            continue
        if rec[cid]:
            r.ok(f"recognitions {name}", f"{rec[cid]} rows in the last {ACTIVITY_WINDOW_S}s")
        else:
            why = top_rejection(health, cid)
            r.warn(f"recognitions {name}", f"no recognition row in the last {ACTIVITY_WINDOW_S}s"
                   + (f"; top rejection: {why}" if why else ""),
                   "normal if nobody faces the camera; otherwise see docs/DEMO_TUNING.md for the "
                   "rejection reason")


async def check_streams(r: Reporter, redis, cameras: dict[str, str], now_ms: int) -> None:
    streams = [f"frames:{c}" for c in cameras] + list(FIXED_STREAMS)
    for stream in streams:
        if not await redis.exists(stream):
            continue
        for g in await redis.xinfo_groups(stream):
            group, pending = _s(g["name"]), int(g["pending"])
            label = f"{stream}/{group}"
            if not pending:
                r.ok(f"pending {label}", "0")
                continue
            summary = await redis.xpending(stream, group)
            age = max(0.0, (now_ms - _ms(summary["min"])) / 1000)
            if age <= MAX_PENDING_AGE_S:
                r.ok(f"pending {label}", f"{pending}, oldest {age:.0f}s")
            elif group in OUR_GROUPS:
                r.fail(f"pending {label}", f"{pending} pending, oldest {age:.0f}s (> {MAX_PENDING_AGE_S}s)",
                       "a service is stuck or down: check /health and its log; restart it "
                       "(pending messages are reclaimed automatically)")
            else:
                r.warn(f"pending {label}", f"{pending} pending, oldest {age:.0f}s (platform group)",
                       "restart the platform service that owns this group")
    dlqs = sorted({_s(k) async for k in redis.scan_iter(match="*:dlq", _type="stream")})
    dead = 0
    for dlq in dlqs:
        n = await redis.xlen(dlq)
        if not n:
            continue
        dead += n
        last = await redis.xrevrange(dlq, count=1)
        err = _s(last[0][1].get(b"error", last[0][1].get("error", ""))) if last else ""
        r.fail(f"dlq {dlq}", f"{n} entries; newest error: {mask_text(err)[:200]}",
               f"inspect: redis-cli XRANGE {dlq} - + COUNT 5 (bad payloads or expired frames)")
    if not dead:
        r.ok("dlq", "no dead-lettered message")
    platform_dead = await redis.xlen("alerts:dead_letter") if await redis.exists("alerts:dead_letter") else 0
    if platform_dead:
        r.warn("alerts:dead_letter", f"{platform_dead} alerts rejected by the platform",
               "inspect: redis-cli XRANGE alerts:dead_letter - + COUNT 5")


# ---------------------------------------------------------------- alerts


async def check_alerts(r: Reporter, redis, known_cams: set[UUID], uc: str, now_ms: int,
                       window_s: int) -> dict[str, float]:
    """Validate our alerts on alerts:live. Returns {alert_id: published epoch seconds}."""
    from pydantic import ValidationError

    from shared.platform_contracts.alert_event import AlertEvent, AlertEventValidator

    entries = await redis.xrange("alerts:live", min=str(now_ms - window_s * 1000), max="+",
                                 count=SCAN_LIMIT)
    ours: dict[str, float] = {}
    seen: Counter = Counter()
    invalid = others = 0
    for msg_id, fields in entries:
        raw = _data(fields)
        try:
            source = json.loads(raw or "{}").get("source_uc")
        except ValueError:
            source = None
        if source is not None and source != uc:
            others += 1
            continue
        try:
            if raw is None:
                raise ValueError("entry has no 'data' field")
            alert = AlertEvent.model_validate_json(raw)
            problems = AlertEventValidator.validate(alert, known_cams)
        except (ValidationError, ValueError) as exc:
            problems = [str(exc).splitlines()[0]]
            alert = None
        if problems:
            invalid += 1
            r.fail(f"alert {_s(msg_id)}", "invalid: " + mask_text("; ".join(problems))[:300],
                   "this is a bug in the use case: collect_diagnostics and report it")
            continue
        aid = str(alert.alert_id)
        seen[aid] += 1
        ours[aid] = _ms(msg_id) / 1000
    dups = {a: n for a, n in seen.items() if n > 1}
    for aid, n in dups.items():
        r.fail(f"alert {aid}", f"published {n} times", "collect_diagnostics and report it")
    if not invalid and not dups:
        r.ok("alerts", f"{len(ours)} {uc} alert(s) in the last {window_s}s, all valid, no duplicate "
             f"alert_id ({others} from other use cases)")
    return ours


async def check_platform_alerts(r: Reporter, platform_factory, ours: dict[str, float], uc: str,
                                window_s: int, *, table: str, id_col: str, uc_col: str,
                                time_col: str, slack_s: int, grace_s: int, now: float) -> None:
    from sqlalchemy import text

    for ident in (table, id_col, uc_col, time_col):
        if not IDENT.match(ident):
            r.fail("platform alerts", f"invalid identifier {ident!r}", "use a plain table/column name")
            return
    since = datetime.fromtimestamp(now - window_s - slack_s, tz=timezone.utc)
    try:
        async with platform_factory() as s:
            async with s.begin():
                await s.execute(text("SET TRANSACTION READ ONLY"))
                rows = await s.execute(text(
                    f"SELECT CAST({id_col} AS text) AS alert_id FROM {table} "
                    f"WHERE {uc_col} = :uc AND {time_col} >= :since"), {"uc": uc, "since": since})
                stored = {row.alert_id for row in rows}
    except Exception as exc:
        r.fail("platform alerts", f"cannot read {table}: {mask_text(str(exc)).splitlines()[0][:200]}",
               "check PLATFORM_DATABASE_URL in .env (read-only use); see docs/INTEGRATION.md")
        return
    due = {a for a, t in ours.items() if t <= now - grace_s}
    missing = sorted(due - stored)
    if missing:
        r.fail("platform alerts", f"{len(missing)} of {len(due)} published alert(s) not stored by the "
               f"platform, e.g. {', '.join(missing[:3])}",
               "Alert Management is not consuming: restart it (docker compose ... restart <alert "
               "management service>) and inspect alerts:dead_letter")
    else:
        r.ok("platform alerts", f"all {len(due)} published alert(s) stored by the platform "
             f"({len(stored)} {uc} row(s) in {table} since {since:%H:%M:%S} UTC)")


# ---------------------------------------------------------------- main


async def run(r: Reporter, env: dict, redis, session_factory, platform_factory, *, window_s: int,
              health_ports=HEALTH, registry: RegistryClient | None = None, now: float | None = None,
              table="alerts", id_col="alert_id", uc_col="source_uc", time_col="created_at",
              slack_s=120, grace_s=10) -> None:
    now = time.time() if now is None else now
    now_ms = int(now * 1000)
    uc = env.get("SOURCE_UC", "uc1")
    registry = registry or RegistryClient.from_env(env)
    try:
        ids = registry.by_uc(uc)
    except RegistryError as exc:
        r.fail("registry", str(exc), exc.fix)
        ids = []
    names: dict[str, str] = {}
    known: set[UUID] = set()
    try:
        active = registry.active_cameras()
        names = {c.id: c.name for c in active}
        known = {UUID(c.id) for c in active}
    except RegistryError as exc:
        r.fail("registry", str(exc), exc.fix)
    cameras = {cid: names.get(cid, "?") for cid in ids}
    if ids:
        r.ok("cameras", f"{len(ids)} for {uc}: " + ", ".join(f"{n!r}" for n in cameras.values()))
    elif not r.failed:
        r.fail("cameras", f"/cameras/by-uc/{uc} lists no camera", "scripts\\register_cameras.ps1")

    health = fetch_health(health_ports)
    check_health(r, health)
    await check_cameras(r, redis, session_factory, cameras, health, now_ms)
    await check_streams(r, redis, cameras, now_ms)
    ours = await check_alerts(r, redis, known, uc, now_ms, window_s)
    if platform_factory is None:
        r.warn("platform alerts", "PLATFORM_DATABASE_URL not set: stored alerts not compared",
               "set PLATFORM_DATABASE_URL in .env (read-only use)")
    else:
        await check_platform_alerts(r, platform_factory, ours, uc, window_s, table=table, id_col=id_col,
                                    uc_col=uc_col, time_col=time_col, slack_s=slack_s,
                                    grace_s=grace_s, now=now)


async def _main(args) -> int:
    import redis.asyncio as aioredis
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    env = load_env()
    r = Reporter()
    redis = aioredis.from_url(
        f"redis://{env.get('REDIS_HOST', 'localhost')}:{env.get('REDIS_PORT', 6379)}/{env.get('REDIS_DB', 0)}",
        socket_timeout=5)
    engines = []

    def factory(url):
        if not url:
            return None
        e = create_async_engine(url)
        engines.append(e)
        return async_sessionmaker(e, expire_on_commit=False)

    try:
        await redis.ping()
    except Exception as exc:
        r.fail("redis", f"unreachable: {exc}", "start the platform stack")
        await redis.aclose()
        return r.summary()
    try:
        await run(r, env, redis, factory(env.get("DATABASE_URL")), factory(env.get("PLATFORM_DATABASE_URL")),
                  window_s=args.window, table=args.alerts_table, id_col=args.alert_id_column,
                  uc_col=args.source_uc_column, time_col=args.created_at_column,
                  slack_s=args.slack, grace_s=args.grace)
    finally:
        await redis.aclose()
        for e in engines:
            await e.dispose()
    return r.summary()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--window", type=int, default=600, help="seconds of alerts:live to check")
    ap.add_argument("--alerts-table", default="alerts")
    ap.add_argument("--alert-id-column", default="alert_id")
    ap.add_argument("--source-uc-column", default="source_uc")
    ap.add_argument("--created-at-column", default="created_at",
                    help="holds the alert's EVENT timestamp, hence --slack")
    ap.add_argument("--slack", type=int, default=120, help="extra seconds before the window (event time)")
    ap.add_argument("--grace", type=int, default=10,
                    help="alerts younger than this are not required to be stored yet")
    return asyncio.run(_main(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
