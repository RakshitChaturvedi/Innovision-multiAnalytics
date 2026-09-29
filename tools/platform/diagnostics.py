"""Collect everything needed to debug a failed run into diagnostics_<timestamp>.zip.

    python -m tools.platform.diagnostics --platform-path C:\\innovision\\platform
    (scripts\\collect_diagnostics.ps1 -PlatformPath <dir>)

Contents (all text, all masked: tokens, passwords, keys, credentials in URLs):
  logs/<service>.log         last 500 lines of each use case log
  check_integration.txt      output of tools/check_integration.py
  health/<service>.json      GET /health of the three services
  streams.txt                stream lengths, groups, pending, DLQs (read-only)
  alembic.txt                alembic heads + current
  docker_compose_ps.txt      docker compose ps of the platform stack
  platform_logs/<svc>.txt    last 300 lines of ingestion, camera registry, alert management
  env/usecase.env, env/platform.env   both .env files, secrets masked
Never included: model files, photos, snapshots, anything that is not text we produced.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from tools.platform.common import (
    ROOT, Reporter, compose_services, find_service, load_env, mask_env_text, mask_text,
    platform_compose,
)

LOG_LINES = 500
PLATFORM_LOG_LINES = 300
ALLOWED_SUFFIXES = (".txt", ".log", ".json", ".env")
PLATFORM_SERVICES = (("ingestion", ("ingest",)), ("camera_registry", ("camera", "regist")),
                     ("alert_management", ("alert", "manag")))


def run(cmd: list[str], timeout: float = 120) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=ROOT,
                           env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"},
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return f"({cmd[0]} not found)\n"
    except subprocess.TimeoutExpired:
        return f"(timed out after {timeout:.0f}s: {' '.join(cmd[:4])} ...)\n"
    return (p.stdout or "") + (p.stderr or "") + f"\n(exit code {p.returncode})\n"


def tail_lines(path: Path, n: int) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return "".join(lines[-n:])


async def streams_report(env: dict) -> str:
    import redis.asyncio as aioredis

    redis = aioredis.from_url(
        f"redis://{env.get('REDIS_HOST', 'localhost')}:{env.get('REDIS_PORT', 6379)}/{env.get('REDIS_DB', 0)}",
        socket_timeout=5)
    out = []
    try:
        keys = sorted({k.decode() if isinstance(k, bytes) else k
                       async for k in redis.scan_iter(match="*", _type="stream", count=1000)})
        for key in keys:
            if key.startswith(("frames:", "events:", "alerts:")) or key.endswith(":dlq"):
                out.append(f"{key} len={await redis.xlen(key)}")
                for g in await redis.xinfo_groups(key):
                    name = g["name"].decode() if isinstance(g["name"], bytes) else g["name"]
                    oldest = ""
                    if int(g["pending"]):
                        summary = await redis.xpending(key, name)
                        mid = summary["min"].decode() if isinstance(summary["min"], bytes) else summary["min"]
                        oldest = f" oldest_pending_age_s={(time.time() * 1000 - int(mid.split('-')[0])) / 1000:.0f}"
                    out.append(f"  group {name} pending={g['pending']} lag={g.get('lag')} "
                               f"last_delivered={g.get('last-delivered-id')}{oldest}")
                if key.endswith(":dlq") or key == "alerts:dead_letter":
                    for eid, fields in await redis.xrevrange(key, count=5):
                        err = fields.get(b"error", fields.get(b"reason", b""))
                        out.append(f"  last {eid.decode()}: {err.decode(errors='replace')[:300]}")
    except Exception as exc:
        out.append(f"(redis error: {exc})")
    finally:
        await redis.aclose()
    return "\n".join(out) + "\n"


def health_reports() -> dict[str, str]:
    from tools.platform.common import HttpError, http_json

    out = {}
    for name, port in (("detection", 8081), ("recognition", 8082), ("event_processing", 8083)):
        try:
            status, body = http_json("GET", f"http://127.0.0.1:{port}/health", timeout=3)
            out[name] = json.dumps({"http_status": status, "body": body}, indent=2, default=str)
        except HttpError as exc:
            out[name] = json.dumps({"error": str(exc)})
    return out


class Bundle:
    """Text-only zip; every entry is masked on the way in."""

    def __init__(self, path: Path):
        self.path = path
        self.zip = zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED)
        self.names: list[str] = []

    def add(self, name: str, content: str, *, env: bool = False) -> None:
        if not name.endswith(ALLOWED_SUFFIXES):
            raise ValueError(f"refusing non-text entry {name}")
        self.zip.writestr(name, mask_env_text(content) if env else mask_text(content))
        self.names.append(name)

    def close(self) -> None:
        self.zip.close()


def collect(r: Reporter, out: Path, platform_path: Path | None, *, runner=run, env: dict | None = None,
            streams=None, health=health_reports, logs_dir: Path | None = None,
            usecase_env: Path | None = None) -> Path:
    env = env if env is not None else load_env()
    logs_dir = logs_dir or ROOT / "logs"
    b = Bundle(out)
    try:
        logs = sorted(logs_dir.glob("*.log")) if logs_dir.is_dir() else []
        for log in logs:
            b.add(f"logs/{log.name}", tail_lines(log, LOG_LINES))
        r.ok("logs", f"{len(logs)} use case log(s), last {LOG_LINES} lines each")

        b.add("check_integration.txt", runner([sys.executable, "-m", "tools.check_integration"]))
        r.ok("check_integration", "captured")

        for name, body in health().items():
            b.add(f"health/{name}.json", body)
        r.ok("health", "captured")

        text = asyncio.run(streams(env)) if streams else asyncio.run(streams_report(env))
        b.add("streams.txt", text)
        r.ok("streams", "captured")

        b.add("alembic.txt", runner([sys.executable, "-m", "alembic", "-c", "migrations/alembic.ini", "heads"])
              + runner([sys.executable, "-m", "alembic", "-c", "migrations/alembic.ini", "current"]))
        r.ok("alembic", "captured")

        env_file = usecase_env or ROOT / ".env"
        b.add("env/usecase.env", env_file.read_text(encoding="utf-8") if env_file.exists() else "(missing)\n",
              env=True)
        if platform_path is None:
            r.warn("platform", "no -PlatformPath: platform compose ps, logs and .env not collected",
                   "re-run with -PlatformPath <platform repo folder>")
        else:
            compose = platform_compose(platform_path)
            b.add("docker_compose_ps.txt", runner(["docker", "compose", "-f", str(compose), "ps"]))
            services = compose_services(compose) if compose.exists() else {}
            for label, words in PLATFORM_SERVICES:
                svc = find_service(services, *words)
                if svc is None:
                    b.add(f"platform_logs/{label}.txt", f"(no service matching {label} in {compose})\n")
                    continue
                b.add(f"platform_logs/{label}.txt",
                      runner(["docker", "compose", "-f", str(compose), "logs", "--no-color",
                              "--tail", str(PLATFORM_LOG_LINES), svc]))
            penv = Path(platform_path) / ".env"
            b.add("env/platform.env", penv.read_text(encoding="utf-8") if penv.exists() else "(missing)\n",
                  env=True)
            r.ok("platform", "compose ps, 3 service logs, .env (masked)")
    finally:
        b.close()
    r.ok("diagnostics", f"{out} ({len(b.names)} files, secrets masked). Send this file.")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--platform-path", help="platform repo folder")
    ap.add_argument("--out", help="zip path (default diagnostics_<timestamp>.zip)")
    args = ap.parse_args(argv)
    r = Reporter()
    out = Path(args.out or ROOT / f"diagnostics_{time.strftime('%Y%m%d_%H%M%S')}.zip")
    collect(r, out, Path(args.platform_path) if args.platform_path else None)
    return r.summary()


if __name__ == "__main__":
    raise SystemExit(main())
