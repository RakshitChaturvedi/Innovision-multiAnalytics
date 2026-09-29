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
