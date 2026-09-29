"""Everything that must be true before the use case runs against the real platform.

    python -m tools.platform.prereqs --platform-path C:\\innovision\\platform
    (scripts\\check_prereqs.ps1 -PlatformPath <dir>)

One PASS/WARN/FAIL line per check, FAIL lines carry a FIX; exit 1 on any FAIL.
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from tools.platform.common import (
    ROOT, RegistryClient, RegistryError, Reporter, compose_services, platform_compose,
    read_env_file, tcp_reachable,
)

VENV_PACKAGES = ("redis", "sqlalchemy", "asyncpg", "pgvector", "alembic", "pydantic_settings",
                 "minio", "cv2", "numpy", "yaml", "torch", "ultralytics", "insightface",
                 "onnxruntime")
PLATFORM_MINIO_KEYS = {"MINIO_ACCESS_KEY": ("MINIO_ACCESS_KEY", "MINIO_ROOT_USER"),
                       "MINIO_SECRET_KEY": ("MINIO_SECRET_KEY", "MINIO_ROOT_PASSWORD")}


def run(cmd: list[str], timeout: float = 20) -> tuple[int, str]:
    """(exit code, stdout+stderr); 127 if the program is missing."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]} timed out after {timeout:.0f}s"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# ---------------------------------------------------------------- stubs


def running_containers(runner=run) -> list[tuple[str, str, str]] | None:
    """[(container name, compose service, compose config files)] or None if docker fails."""
    fmt = ('{{.Names}}|{{.Label "com.docker.compose.service"}}|'
           '{{.Label "com.docker.compose.project.config_files"}}')
    code, out = runner(["docker", "ps", "--format", fmt])
    if code != 0:
        return None
    rows = []
    for line in out.splitlines():
        parts = (line.split("|") + ["", ""])[:3]
        if parts[0].strip():
            rows.append((parts[0].strip(), parts[1].strip(), parts[2].strip()))
    return rows


def running_stub_containers(platform_path: Path | None, runner=run) -> list[str] | None:
    """Containers started from docker-compose.stubs.yml (by compose label), or whose
    container_name only the stubs file defines. None if docker cannot be asked."""
    stub_names: set[str] = set()
    stubs_file = platform_compose(platform_path, stubs=True) if platform_path else None
    if stubs_file is not None and stubs_file.exists():
        main_file = platform_compose(platform_path)
        main = compose_services(main_file) if main_file.exists() else {}
        main_containers = {c for c in main.values() if c}
        stub_names = {c for c in compose_services(stubs_file).values()
                      if c and c not in main_containers}
    rows = running_containers(runner)
    if rows is None:
        return None
    return sorted({name for name, _svc, files in rows
                   if "docker-compose.stubs.yml" in files.replace("\\", "/") or name in stub_names})


def check_stubs(r: Reporter, platform_path: Path | None, runner=run) -> None:
    stubs = running_stub_containers(platform_path, runner)
    if stubs is None:
        r.fail("stubs", "cannot list containers (docker ps failed)", "start Docker Desktop")
    elif stubs:
        r.fail("stubs", f"stub containers running: {', '.join(stubs)}",
               f"docker compose -f {platform_compose(platform_path or '<PlatformPath>', stubs=True)} down "
               "(the stubs must never run with the use case)")
    else:
        r.ok("stubs", "no container from docker-compose.stubs.yml is running")


# ---------------------------------------------------------------- checks


def check_tools(r: Reporter, runner=run, version=sys.version_info, find_spec=importlib.util.find_spec,
                which=shutil.which) -> None:
    code, out = runner(["docker", "info", "--format", "{{.ServerVersion}}"])
    if code == 0:
        r.ok("docker", f"Docker running (server {out.strip()})")
    else:
        r.fail("docker", "Docker is not running", "start Docker Desktop and wait until it says 'running'")

    v = f"{version[0]}.{version[1]}.{version[2]}"
    if tuple(version[:2]) == (3, 12):
        r.ok("python", f"Python {v} ({sys.executable})")
    else:
        r.fail("python", f"Python {v}, need 3.12",
               "create the venv with Python 3.12: py -3.12 -m venv .venv")

    missing = [m for m in VENV_PACKAGES if find_spec(m) is None]
    if missing:
        r.fail("venv", f"missing packages: {', '.join(missing)}",
               ".venv\\Scripts\\python -m pip install -e . and the service requirements "
               "(services\\*\\requirements.txt)")
    else:
        r.ok("venv", f"{len(VENV_PACKAGES)} packages importable")

    for exe in ("ffmpeg", "ffprobe"):
        if which(exe):
            r.ok(exe, which(exe))
        else:
            r.fail(exe, f"{exe} not on PATH",
                   "winget install Gyan.FFmpeg, then open a NEW PowerShell window")


def check_git(r: Reporter, name: str, path: Path, runner=run) -> None:
    code, out = runner(["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"])
    if code != 0:
        r.warn(f"git {name}", f"not a git checkout: {path}")
        return
    branch = out.strip()
    code, head = runner(["git", "-C", str(path), "log", "-1", "--format=%h %s"])
    r.ok(f"git {name}", f"branch {branch} ({head.strip()[:60]})")


def _first(env: dict, keys) -> tuple[str | None, str]:
    for k in keys:
        if env.get(k):
            return k, env[k]
    return None, ""


def check_env_files(r: Reporter, ours: Path, platform: Path) -> tuple[dict, dict]:
    envs = []
    for label, p in (("this repo", ours), ("platform", platform)):
        if p.exists():
            r.ok(f".env {label}", str(p))
            envs.append(read_env_file(p))
        else:
            fix = ("copy .env.platform.example to .env and fill in the <...> values"
                   if label == "this repo" else "create the platform .env (see the platform README)")
            r.fail(f".env {label}", f"missing: {p}", fix)
            envs.append({})
    mine, plat = envs
    if not (ours.exists() and platform.exists()):
        return mine, plat

    tok, ptok = mine.get("INTERNAL_SERVICE_TOKEN", ""), plat.get("INTERNAL_SERVICE_TOKEN", "")
    if not ptok:
        r.fail("service token", "INTERNAL_SERVICE_TOKEN is missing/empty in the platform .env",
               "add INTERNAL_SERVICE_TOKEN=<long random value> to the platform .env AND pass it to "
               "camera_registry in the platform docker-compose.yml, then restart it "
               "(docs/INTEGRATION.md, Known platform issues)")
    elif not tok:
        r.fail("service token", "INTERNAL_SERVICE_TOKEN is empty in this repo's .env",
               "copy the value from the platform .env")
    elif tok != ptok:
        r.fail("service token", "INTERNAL_SERVICE_TOKEN differs between the two .env files",
               "copy the value from the platform .env into this repo's .env")
    else:
        r.ok("service token", "set and equal in both .env files")

    for key, pkeys in PLATFORM_MINIO_KEYS.items():
        pk, pv = _first(plat, pkeys)
        if not mine.get(key) or not pv:
            r.fail(key, f"missing in {'this repo' if not mine.get(key) else 'the platform'} .env",
                   f"set {key} in this repo's .env to the platform's {pk or pkeys[-1]}")
        elif mine[key] != pv:
            r.fail(key, f"differs from the platform's {pk}",
                   f"copy the platform's {pk} value into {key} in this repo's .env")
        else:
            r.ok(key, f"equal to the platform's {pk}")
    return mine, plat


def check_models(r: Reporter, env: dict) -> None:
    weights = env.get("YOLOV11M_PATH", "")
    if weights and Path(weights).is_file():
        r.ok("yolo weights", weights)
    else:
        r.fail("yolo weights", f"not found: {weights or '(YOLOV11M_PATH unset)'}",
               "set YOLOV11M_PATH in .env to the trained yolov11m .pt file")
    root, pack = env.get("MODEL_ROOT", ""), env.get("RECOGNITION_MODEL_PACK", "buffalo_s")
    pack_dir = Path(root) / "models" / pack
    onnx = sorted(pack_dir.glob("*.onnx")) if root and pack_dir.is_dir() else []
    if onnx:
        r.ok("face models", f"{pack_dir} ({len(onnx)} .onnx)")
    else:
        r.fail("face models", f"no .onnx files in {pack_dir}",
               f"MODEL_ROOT must contain models\\{pack}\\*.onnx (set MODEL_ROOT / "
               "RECOGNITION_MODEL_PACK in .env)")


def _hostport(url: str, default_port: int) -> tuple[str, int]:
    u = urlsplit(url if "://" in url else f"x://{url}")
    return u.hostname or "localhost", u.port or default_port


def check_services(r: Reporter, env: dict, uc: str | None = None) -> RegistryClient | None:
    """Redis, Postgres, MinIO, registry, auth. Returns the registry client if reachable."""
    host, port = env.get("REDIS_HOST", "localhost"), int(env.get("REDIS_PORT", 6379))
    err = tcp_reachable(host, port)
    if err is None:
        try:
            import redis

            redis.Redis(host=host, port=port, db=int(env.get("REDIS_DB", 0)),
                        socket_timeout=3).ping()
            r.ok("redis", f"{host}:{port} db {env.get('REDIS_DB', 0)} answers PING")
        except Exception as exc:  # auth, protocol: say what happened
            r.fail("redis", f"{host}:{port} refused PING: {exc}",
                   "check REDIS_HOST/REDIS_PORT in .env point at the platform Redis")
    else:
        r.fail("redis", f"{host}:{port} unreachable ({err})",
               "start the platform stack; REDIS_HOST=localhost, REDIS_PORT=6379")

    pg = env.get("DATABASE_URL", "")
    h, p = _hostport(pg, 5432)
    err = tcp_reachable(h, p)
    if err is None:
        r.ok("postgres", f"{h}:{p} reachable")
    else:
        r.fail("postgres", f"{h}:{p} unreachable ({err})",
               "start the platform stack; DATABASE_URL must use the platform Postgres host/port")

    h, p = _hostport(env.get("MINIO_ENDPOINT", "localhost:9000"), 9000)
    err = tcp_reachable(h, p)
    if err is None:
        r.ok("minio", f"{h}:{p} reachable")
    else:
        r.fail("minio", f"{h}:{p} unreachable ({err})",
               "start the platform stack; MINIO_ENDPOINT=localhost:9000")

    h, p = _hostport(env.get("PLATFORM_AUTH_URL", "http://localhost:8000"), 8000)
    err = tcp_reachable(h, p)
    if err is None:
        r.ok("auth", f"{h}:{p} reachable")
    else:
        r.fail("auth", f"{h}:{p} unreachable ({err})",
               "start the platform stack; PLATFORM_AUTH_URL=http://localhost:8000")

    client = RegistryClient.from_env(env)
    uc = uc or env.get("SOURCE_UC", "uc1")
    try:
        ids = client.by_uc(uc)
        r.ok("registry", f"{client.registry} answers, {len(ids)} camera(s) for {uc}")
        check_test_cameras(r, client, ids, uc)
        return client
    except RegistryError as exc:
        r.fail("registry", str(exc), exc.fix)
        return None


def check_test_cameras(r: Reporter, client: RegistryClient, ids: list[str], uc: str) -> None:
    """WARN when the platform's built-in "Test Camera*" (video files) are still
    subscribed to our use case: detection would process them too."""
    if not ids:
        return
    try:
        names = {c.id: c.name for c in client.active_cameras()}
    except RegistryError as exc:
        r.warn("test cameras", f"cannot read camera names: {exc}", exc.fix)
        return
    test = sorted(names[i] for i in ids if names.get(i, "").startswith("Test Camera"))
    if test:
        r.warn("test cameras", f"/cameras/by-uc/{uc} lists platform test camera(s): {', '.join(test)}",
               "scripts\\unsubscribe_test_cameras.ps1 (docs/INTEGRATION.md), then restart Alert Management")
    else:
        r.ok("test cameras", f"none subscribed to {uc}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--platform-path", required=True, help="the platform repo checkout")
    args = ap.parse_args(argv)
    platform = Path(args.platform_path)
    r = Reporter()
    if not platform_compose(platform).exists():
        r.fail("platform path", f"{platform_compose(platform)} not found",
               "pass -PlatformPath <platform repo folder> (it contains infra\\docker-compose.yml)")
    check_tools(r)
    check_git(r, "use case", ROOT)
    check_git(r, "platform", platform)
    mine, _ = check_env_files(r, ROOT / ".env", platform / ".env")
    check_models(r, mine)
    check_services(r, mine)
    check_stubs(r, platform)
    return r.summary()


if __name__ == "__main__":
    raise SystemExit(main())
