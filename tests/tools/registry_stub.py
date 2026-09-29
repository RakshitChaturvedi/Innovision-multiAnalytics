"""In-process stand-in for the platform auth + camera registry endpoints,
shaped exactly like the platform's (docs/INTEGRATION.md):

  POST /auth/login?email=&password=          -> {"access_token", "token_type": "bearer"}
  POST /cameras                 (Bearer)     -> 201 camera incl. "id"
  PUT  /cameras/{id}/config     (Bearer)     -> {"status": "updated"}
  GET  /cameras/internal/active (X-Service-Token) -> [camera, ...]
  GET  /cameras/by-uc/{uc}                   -> {"uc_id", "camera_ids": [...]}
"""
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from tests.tools import fakes

ADMIN = ("admin@example.invalid", fakes.fake("admin-login"))
SERVICE_TOKEN = fakes.fake("service-token")
BEARER = fakes.fake("bearer")


class RegistryStub:
    def __init__(self):
        self.cameras: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def _admin(self):
                return self.headers.get("Authorization") == f"Bearer {BEARER}"

            def do_POST(self):
                parts = urlsplit(self.path)
                stub.calls.append(("POST", parts.path))
                if parts.path == "/auth/login":
                    q = parse_qs(parts.query)
                    if (q.get("email", [""])[0], q.get("password", [""])[0]) == ADMIN:
                        return self._send(200, {"access_token": BEARER, "token_type": "bearer"})
                    return self._send(401, {"detail": "Invalid credentials"})
                if parts.path == "/cameras":
                    if not self._admin():
                        return self._send(401, {"detail": "Not authenticated"})
                    b = self._body()
                    cam = {"id": str(uuid.uuid4()), "name": b["name"], "location": b["location"],
                           "rtsp_url": b["rtsp_url"], "use_cases": b["use_cases"],
                           "fps": b["fps"], "status": "offline"}
                    stub.cameras[cam["id"]] = cam
                    return self._send(201, cam)
                self._send(404, {"detail": "Not Found"})

            def do_PUT(self):
                path = urlsplit(self.path).path
                stub.calls.append(("PUT", path))
                p = path.strip("/").split("/")
                if len(p) == 3 and p[0] == "cameras" and p[2] == "config":
                    if not self._admin():
                        return self._send(401, {"detail": "Not authenticated"})
                    cam = stub.cameras.get(p[1])
                    if cam is None:
                        return self._send(404, {"detail": "Camera not found"})
                    b = self._body()
                    for k in ("use_cases", "fps", "rtsp_url"):
                        if k in b:
                            cam[k] = b[k]
                    return self._send(200, {"status": "updated"})
                self._send(404, {"detail": "Not Found"})

            def do_GET(self):
                path = urlsplit(self.path).path
                stub.calls.append(("GET", path))
                if path == "/cameras/internal/active":
                    if self.headers.get("X-Service-Token") != SERVICE_TOKEN:
                        return self._send(403, {"detail": "Forbidden"})
                    return self._send(200, list(stub.cameras.values()))
                if path.startswith("/cameras/by-uc/"):
                    uc = path.rsplit("/", 1)[1]
                    ids = [c["id"] for c in stub.cameras.values() if uc in c["use_cases"]]
                    return self._send(200, {"uc_id": uc, "camera_ids": ids})
                self._send(404, {"detail": "Not Found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def add(self, name, use_cases=("uc1",), rtsp_url="http://10.0.0.5:4747/video", fps=5):
        cam = {"id": str(uuid.uuid4()), "name": name, "location": "Lobby",
               "rtsp_url": rtsp_url, "use_cases": list(use_cases), "fps": fps}
        self.cameras[cam["id"]] = cam
        return cam

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def client(self, token=SERVICE_TOKEN):
        from tools.platform.common import RegistryClient

        return RegistryClient(self.url, self.url, token, timeout=5)
