"""Shared helpers for the platform wiring tools (scripts/*.ps1 are thin wrappers).

Output contract: every check prints one line
    PASS <name>: <detail>
    WARN <name>: <detail>
    FAIL <name>: <detail>
         FIX: <what to do>
and the tool exits 1 if anything FAILed. Secrets are never printed: every URL
goes through mask_url(), every env value through mask_env_text().
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------- reporting


@dataclass
class Reporter:
    out: object = None
    failed: int = 0
    warned: int = 0
    lines: list[str] = field(default_factory=list)

    def _emit(self, line: str) -> None:
        self.lines.append(line)
        print(line, file=self.out or sys.stdout, flush=True)

    def ok(self, name: str, detail: str = "") -> None:
        self._emit(f"PASS {name}: {detail}".rstrip(": "))

    def warn(self, name: str, detail: str, fix: str | None = None) -> None:
        self.warned += 1
        self._emit(f"WARN {name}: {detail}")
        if fix:
            self._emit(f"     FIX: {fix}")

    def fail(self, name: str, detail: str, fix: str) -> None:
        self.failed += 1
        self._emit(f"FAIL {name}: {detail}")
        self._emit(f"     FIX: {fix}")

    def info(self, text: str) -> None:
        self._emit(text)

    def exit_code(self) -> int:
        return 1 if self.failed else 0

    def summary(self) -> int:
        self._emit(
            "RESULT: FAIL (%d failed, %d warnings)" % (self.failed, self.warned)
            if self.failed else f"RESULT: PASS ({self.warned} warnings)"
        )
        return self.exit_code()


# ---------------------------------------------------------------- env

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def read_env_file(path: Path | str) -> dict[str, str]:
    """Parse a .env file (KEY=VALUE, # comments, optional quotes)."""
    values: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
        m = _ENV_LINE.match(raw)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        else:
            val = re.split(r"\s+#", val, maxsplit=1)[0].strip()
        values[key] = val
    return values


def load_env(path: Path | str | None = None) -> dict[str, str]:
    """The repo .env overlaid by the process environment (like the services)."""
    p = Path(path) if path else ROOT / ".env"
    values = read_env_file(p) if p.exists() else {}
    values.update({k: v for k, v in os.environ.items() if k in values or k.isupper()})
    return values


# ---------------------------------------------------------------- masking

SECRET_KEY = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|PASS|KEY|CREDENTIAL|AUTH)", re.I)
_URL = re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+)")
_SECRET_PARAM = re.compile(r"(?i)((?:token|password|passwd|pwd|pass|secret|key|auth|user|username|email)=)[^&\s]*")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_KV_SECRET = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|KEY|CREDENTIAL)[A-Z0-9_]*)(\s*[=:]\s*)(\"?)[^\s\"]+"
)
MASK = "***"


def mask_url(url: str) -> str:
    """Hide user:password@ and secret-looking query parameters."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return MASK
    netloc = parts.netloc
    if "@" in netloc:
        netloc = f"{MASK}@{netloc.rsplit('@', 1)[1]}"
    query = _SECRET_PARAM.sub(lambda m: m.group(1) + MASK, parts.query)
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def mask_text(text: str) -> str:
    """Mask URLs with credentials, bearer tokens and KEY=secret pairs in free text."""
    text = _URL.sub(lambda m: mask_url(m.group(1)), text)
    text = _BEARER.sub(lambda m: m.group(1) + MASK, text)
    return _KV_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{MASK}", text)


def mask_env_text(text: str) -> str:
    """A whole .env file: secret keys lose their value, URLs lose credentials."""
    out = []
    for line in text.splitlines():
        m = _ENV_LINE.match(line)
        if m and not line.lstrip().startswith("#"):
            key, val = m.group(1), m.group(2)
            if SECRET_KEY.search(key) and val.strip():
                line = f"{key}={MASK}"
            else:
                line = f"{key}={mask_text(val)}"
        else:
            line = mask_text(line)
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


# ---------------------------------------------------------------- network


def tcp_reachable(host: str, port: int, timeout: float = 3.0) -> str | None:
    """None if a TCP connection succeeds, else the error text."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return None
    except OSError as exc:
        return str(exc)


class HttpError(Exception):
    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status


def http_json(method: str, url: str, *, headers: dict | None = None, body=None,
              timeout: float = 10.0):
    """(status, parsed JSON or text). Raises HttpError on connection errors."""
    data = None
    hdrs = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise HttpError(None, f"{mask_url(url)} unreachable: {reason}") from exc
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text) if text else None
    except ValueError:
        return status, text


# ---------------------------------------------------------------- registry


@dataclass
class Camera:
    id: str
    name: str
    raw: dict

    @property
    def rtsp_url(self) -> str | None:
        return self.raw.get("rtsp_url")

    @property
    def use_cases(self) -> list[str]:
        return list(self.raw.get("use_cases") or [])

    @property
    def fps(self):
        return self.raw.get("fps")


class RegistryError(Exception):
    """Carries a FIX hint for the caller's FAIL line."""

    def __init__(self, message: str, fix: str):
        super().__init__(message)
        self.fix = fix


class RegistryClient:
    """The platform camera registry + auth service.

    POST {auth}/auth/login?email=&password=      -> {"access_token", "token_type"}
    POST {registry}/cameras              (Bearer)  -> 201 camera incl. "id"
    PUT  {registry}/cameras/{id}/config  (Bearer)  -> {"status": "updated"}
    GET  {registry}/cameras/internal/active (X-Service-Token) -> [camera, ...]
    GET  {registry}/cameras/by-uc/{uc}              -> {"uc_id", "camera_ids": [...]}
    """

    def __init__(self, registry_url: str, auth_url: str | None = None,
                 service_token: str | None = None, timeout: float = 10.0):
        self.registry = registry_url.rstrip("/")
        self.auth = (auth_url or "").rstrip("/")
        self.service_token = service_token or ""
        self.timeout = timeout
        self.bearer: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "RegistryClient":
        return cls(
            env.get("CAMERA_REGISTRY_URL", "http://localhost:8011"),
            env.get("PLATFORM_AUTH_URL", "http://localhost:8000"),
            env.get("INTERNAL_SERVICE_TOKEN", ""),
        )

    def _call(self, method, url, **kw):
        try:
            return http_json(method, url, timeout=self.timeout, **kw)
        except HttpError as exc:
            raise RegistryError(str(exc), "start the platform stack (docker compose -f "
                                "<PlatformPath>\\infra\\docker-compose.yml up -d) and check "
                                "CAMERA_REGISTRY_URL / PLATFORM_AUTH_URL in .env") from exc

    def login(self, email: str, password: str) -> None:
        url = f"{self.auth}/auth/login?" + urlencode({"email": email, "password": password})
        status, body = self._call("POST", url)
        if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
            raise RegistryError(f"login failed (HTTP {status})",
                                "check the platform admin email/password (typed at the prompt)")
        self.bearer = body["access_token"]

    def _auth_headers(self) -> dict:
        if not self.bearer:
            raise RegistryError("not logged in", "call login() first")
        return {"Authorization": f"Bearer {self.bearer}"}

    def active_cameras(self) -> list[Camera]:
        if not self.service_token:
            raise RegistryError("INTERNAL_SERVICE_TOKEN is empty",
                                "copy INTERNAL_SERVICE_TOKEN from the platform .env into this repo's .env")
        status, body = self._call("GET", f"{self.registry}/cameras/internal/active",
                                  headers={"X-Service-Token": self.service_token})
        if status in (401, 403):
            raise RegistryError(f"internal endpoint refused the service token (HTTP {status})",
                                "INTERNAL_SERVICE_TOKEN must equal the platform .env value; restart "
                                "camera_registry after changing it there")
        if status != 200:
            raise RegistryError(f"GET /cameras/internal/active -> HTTP {status}",
                                "check the camera_registry container logs")
        items = body.get("cameras", body) if isinstance(body, dict) else body
        if not isinstance(items, list):
            raise RegistryError("unexpected /cameras/internal/active body",
                                "check the platform version (expected a list of cameras)")
        return [Camera(str(c.get("id")), str(c.get("name", "")), c)
                for c in items if isinstance(c, dict) and c.get("id")]

    def resolve(self, name: str, cameras: list[Camera] | None = None) -> Camera:
        cams = cameras if cameras is not None else self.active_cameras()
        hits = [c for c in cams if c.name == name]
        if len(hits) == 1:
            return hits[0]
        known = ", ".join(sorted(repr(c.name) for c in cams)) or "(none)"
        if not hits:
            raise RegistryError(f"camera {name!r} not found; registered: {known}",
                                "use one of the names above, or scripts\\register_cameras.ps1")
        raise RegistryError(f"camera name {name!r} is used by {len(hits)} cameras "
                            f"({', '.join(c.id for c in hits)})",
                            "give each camera a unique name in the platform")

    def by_uc(self, uc: str) -> list[str]:
        status, body = self._call("GET", f"{self.registry}/cameras/by-uc/{quote(uc)}")
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("camera_ids"), list):
            raise RegistryError(f"GET /cameras/by-uc/{uc} -> HTTP {status}",
                                "check the camera_registry container logs")
        return [str(c) for c in body["camera_ids"]]

    def create_camera(self, *, name, location, rtsp_url, use_cases, fps) -> Camera:
        status, body = self._call(
            "POST", f"{self.registry}/cameras", headers=self._auth_headers(),
            body={"name": name, "location": location, "rtsp_url": rtsp_url,
                  "use_cases": use_cases, "fps": fps},
        )
        if status != 201 or not isinstance(body, dict) or not body.get("id"):
            detail = mask_text(json.dumps(body)[:300]) if body is not None else ""
            raise RegistryError(f"create camera -> HTTP {status} {detail}",
                                "the account must be a platform admin; check the camera_registry logs")
        return Camera(str(body["id"]), str(body.get("name", name)), body)

    def update_camera(self, camera_id: str, changes: dict) -> None:
        status, body = self._call("PUT", f"{self.registry}/cameras/{quote(camera_id)}/config",
                                  headers=self._auth_headers(), body=changes)
        if status != 200:
            raise RegistryError(f"update camera -> HTTP {status}",
                                "the account must be a platform admin; check the camera_registry logs")


# ---------------------------------------------------------------- compose


def compose_services(compose_file: Path) -> dict[str, str | None]:
    """{service name: container_name or None} from a docker compose file."""
    import yaml

    data = yaml.safe_load(Path(compose_file).read_text(encoding="utf-8")) or {}
    services = data.get("services") or {}
    return {name: (spec or {}).get("container_name") for name, spec in services.items()}


def find_service(services: dict[str, str | None], *words: str) -> str | None:
    """First service whose name contains all `words` (e.g. "alert", "manag")."""
    for name in services:
        norm = name.lower().replace("-", "_")
        if all(w in norm for w in words):
            return name
    return None


def platform_compose(platform_path: Path | str, stubs: bool = False) -> Path:
    name = "docker-compose.stubs.yml" if stubs else "docker-compose.yml"
    return Path(platform_path) / "infra" / name
