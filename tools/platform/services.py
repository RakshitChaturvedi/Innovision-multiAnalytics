"""Start / stop the use case services as background processes.

    python -m tools.platform.services up   [--platform-path DIR] [--wait 60]
    python -m tools.platform.services down [--timeout 20]
    python -m tools.platform.services status
    (scripts\\up.ps1, scripts\\down.ps1)

up:   preflight (Redis, MinIO, registry reachable; INTERNAL_SERVICE_TOKEN set;
      no stub container running; /cameras/by-uc/<SOURCE_UC> lists >= 1 camera),
      then starts detection, recognition and event_processing from this venv.
      Logs: logs/<service>.log, PID: logs/<service>.pid. Each is checked alive and
      its GET /health polled.
down: graceful first (creates logs/<service>.stop, which the service's runner
      watches: INNOVISION_STOP_FILE), then force-kills after --timeout seconds.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from tools.platform.common import (
    ROOT, RegistryClient, RegistryError, Reporter, http_json, HttpError, load_env, tcp_reachable,
)
from tools.platform.prereqs import _hostport, check_stubs

IS_WINDOWS = os.name == "nt"


@dataclass(frozen=True)
class Service:
    name: str
    module: str
    health_port: int


SERVICES = (
    Service("detection", "services.detection.main", 8081),
    Service("recognition", "services.recognition.main", 8082),
    Service("event_processing", "services.event_processing.main", 8083),
)


# ---------------------------------------------------------------- processes


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE = 0x1000, 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie child of this process counts as dead
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        return True


def force_kill(pid: int) -> None:
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


class Paths:
    def __init__(self, logs: Path):
        self.logs = logs

    def log(self, s: Service) -> Path:
        return self.logs / f"{s.name}.log"

    def pid(self, s: Service) -> Path:
        return self.logs / f"{s.name}.pid"

    def stop(self, s: Service) -> Path:
        return self.logs / f"{s.name}.stop"


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def tail(path: Path, n: int = 5) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(no log)"
    from tools.platform.common import mask_text

    return mask_text(" | ".join(lines[-n:]))[:600]


# ---------------------------------------------------------------- preflight


def preflight(r: Reporter, env: dict, platform_path: Path | None) -> list[str]:
    """Returns the camera ids to be processed ([] means do not start)."""
    for label, host_port, default in (("redis", f"{env.get('REDIS_HOST', 'localhost')}:"
                                                f"{env.get('REDIS_PORT', 6379)}", 6379),
                                      ("minio", env.get("MINIO_ENDPOINT", "localhost:9000"), 9000)):
        h, p = _hostport(host_port, default)
        err = tcp_reachable(h, p)
        if err is None:
            r.ok(label, f"{h}:{p} reachable")
        else:
            r.fail(label, f"{h}:{p} unreachable ({err})", "start the platform stack (docs/INTEGRATION.md step 2)")
    if env.get("HEALTH_PORT"):
        r.fail("HEALTH_PORT", "set in .env: all three services would bind the same port",
               "remove HEALTH_PORT from .env (defaults: 8081, 8082, 8083)")
    check_stubs(r, platform_path)
    client = RegistryClient.from_env(env)
    uc = env.get("SOURCE_UC", "uc1")
    try:
        ids = client.by_uc(uc)
    except RegistryError as exc:
        r.fail("registry", str(exc), exc.fix)
        return []
    if not ids:
        r.fail("cameras", f"/cameras/by-uc/{uc} lists no camera",
               "scripts\\register_cameras.ps1, then restart alert_management")
        return []
    try:
        names = {c.id: c.name for c in client.active_cameras()}
        r.ok("service token", "accepted by /cameras/internal/active")
    except RegistryError as exc:
        r.fail("service token", str(exc), exc.fix)
        names = {}
    for cid in ids:
        r.info(f"  camera {names.get(cid, '(name unknown)')!r} {cid}")
    r.ok("cameras", f"{len(ids)} camera(s) for {uc}")
    return ids


# ---------------------------------------------------------------- up / down


def health_ok(port: int) -> tuple[bool, str]:
    try:
        status, body = http_json("GET", f"http://127.0.0.1:{port}/health", timeout=2)
    except HttpError as exc:
        return False, str(exc)
    return status == 200, (body.get("status") if isinstance(body, dict) else str(status))


def start(r: Reporter, paths: Paths, services=SERVICES, *, python: str = sys.executable,
          wait_s: float = 60, health=health_ok) -> None:
    paths.logs.mkdir(parents=True, exist_ok=True)
    started = []
    for s in services:
        pid = read_pid(paths.pid(s))
        if pid and pid_alive(pid):
            r.ok(s.name, f"already running (pid {pid})")
            continue
        paths.pid(s).unlink(missing_ok=True)
        paths.stop(s).unlink(missing_ok=True)
        env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1",
               "INNOVISION_STOP_FILE": str(paths.stop(s))}
        log = open(paths.log(s), "ab")
        kwargs = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                       | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW)
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen([python, "-m", s.module], cwd=ROOT, env=env, stdout=log,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kwargs)
        log.close()
        paths.pid(s).write_text(str(proc.pid))
        started.append((s, proc.pid))
        r.info(f"  started {s.name} pid {proc.pid}, log {paths.log(s)}")

    deadline = time.monotonic() + wait_s
    pending = list(started)
    while pending:
        for s, pid in list(pending):
            if not pid_alive(pid):
                r.fail(s.name, f"exited during startup: {tail(paths.log(s))}",
                       f"read {paths.log(s)}; fix the error, then scripts\\up.ps1 again")
                paths.pid(s).unlink(missing_ok=True)
                pending.remove((s, pid))
                continue
            ok, detail = health(s.health_port)
            if ok:
                r.ok(s.name, f"pid {pid}, /health on :{s.health_port} ok")
                pending.remove((s, pid))
        if not pending:
            break
        if time.monotonic() >= deadline:
            for s, pid in pending:
                r.fail(s.name, f"pid {pid} alive but /health on :{s.health_port} not ok after "
                       f"{wait_s:.0f}s", f"read {paths.log(s)} (model loading can take a while; "
                       "then run tools/check_integration.py)")
            break
        time.sleep(1)


def stop(r: Reporter, paths: Paths, services=SERVICES, *, timeout_s: float = 20) -> None:
    for s in services:
        pid = read_pid(paths.pid(s))
        if not pid or not pid_alive(pid):
            r.ok(s.name, "not running")
            paths.pid(s).unlink(missing_ok=True)
            paths.stop(s).unlink(missing_ok=True)
            continue
        paths.stop(s).write_text("stop")
        if not IS_WINDOWS:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + timeout_s
        while pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.5)
        if pid_alive(pid):
            force_kill(pid)
            time.sleep(0.5)
            if pid_alive(pid):
                r.fail(s.name, f"pid {pid} survived a force kill",
                       f"taskkill /PID {pid} /T /F (or reboot), then delete {paths.pid(s)}")
                continue
            r.warn(s.name, f"pid {pid} did not stop within {timeout_s:.0f}s, force-killed "
                   "(safe: unacked messages are re-delivered)")
        else:
            r.ok(s.name, f"pid {pid} stopped gracefully")
        paths.pid(s).unlink(missing_ok=True)
        paths.stop(s).unlink(missing_ok=True)


def status(r: Reporter, paths: Paths, services=SERVICES) -> None:
    for s in services:
        pid = read_pid(paths.pid(s))
        if pid and pid_alive(pid):
            ok, detail = health_ok(s.health_port)
            (r.ok if ok else r.warn)(s.name, f"pid {pid}, /health {detail}")
        else:
            r.warn(s.name, "not running")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("up", "down", "status"))
    ap.add_argument("--platform-path", help="platform repo folder (stub container names)")
    ap.add_argument("--wait", type=float, default=60, help="seconds to wait for /health after start")
    ap.add_argument("--timeout", type=float, default=20, help="graceful stop timeout (seconds)")
    args = ap.parse_args(argv)
    r = Reporter()
    paths = Paths(ROOT / "logs")
    if args.action == "up":
        env = load_env()
        if not env.get("INTERNAL_SERVICE_TOKEN"):
            r.fail("service token", "INTERNAL_SERVICE_TOKEN is empty in .env",
                   "copy it from the platform .env")
        cams = preflight(r, env, Path(args.platform_path) if args.platform_path else None)
        if r.failed or not cams:
            r.info("Not starting: fix the FAIL lines above.")
            return r.summary()
        start(r, paths, wait_s=args.wait)
    elif args.action == "down":
        stop(r, paths, timeout_s=args.timeout)
    else:
        status(r, paths)
    return r.summary()


if __name__ == "__main__":
    raise SystemExit(main())
