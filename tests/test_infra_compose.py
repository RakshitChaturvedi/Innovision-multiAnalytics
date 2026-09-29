"""MinIO healthcheck must use a tool the minio image ships: mc (curl is not
guaranteed; the release Dockerfile only adds a best-effort static curl)."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def minio_service(path: Path) -> str:
    text = path.read_text()
    match = re.search(r"^  minio:\n(.*?)(?=^  \S|\Z)", text, re.M | re.S)
    assert match, f"no minio service in {path}"
    return match.group(1)


@pytest.mark.parametrize("compose", ["infra/docker-compose.dev.yml", "infra/docker-compose.yaml"])
def test_minio_healthcheck_uses_mc_ready_not_curl(compose):
    service = minio_service(ROOT / compose)
    assert 'test: ["CMD", "mc", "ready", "local"]' in service
    code = "\n".join(line for line in service.splitlines() if not line.strip().startswith("#"))
    assert "curl" not in code
    assert "image: minio/minio:RELEASE.2025-09-07T16-13-09Z" in service


@pytest.mark.parametrize(
    "service,port",
    [("detection", 8081), ("recognition", 8082), ("event_processing", 8083)],
)
def test_app_services_have_health_endpoint_healthcheck(service, port):
    text = (ROOT / "infra/docker-compose.yaml").read_text()
    match = re.search(rf"^  {service}:\n(.*?)(?=^  \S|^\S|\Z)", text, re.M | re.S)
    assert match, f"no {service} service"
    hc = re.search(r"^    healthcheck:\n((?:      .*\n|\s*#.*\n)+)", match.group(1), re.M)
    assert hc, f"{service} has no healthcheck"
    test_line = next(l for l in hc.group(1).splitlines() if l.strip().startswith("test:"))
    # python urllib against /health (curl is not in the service images)
    assert test_line.strip().startswith('test: ["CMD", "python", "-c"')
    assert f"http://127.0.0.1:{port}/health" in test_line
