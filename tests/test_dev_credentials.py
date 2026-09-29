""".env.example, infra/docker-compose.dev.yml and the code defaults agree on
the dev credentials (Postgres analytics/analytics, MinIO minioadmin/minioadmin)
and `make infra` hands the repo-root .env to compose explicitly."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import make_url

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "infra" / "docker-compose.dev.yml"

PG = ("analytics", "analytics")
MINIO = ("minioadmin", "minioadmin")


def env_example() -> dict[str, str]:
    out = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def compose_default(var: str) -> str:
    """The ${VAR:-default} default used in the dev compose file (all uses agree)."""
    defaults = set(re.findall(r"\$\{" + var + r":-([^}]*)\}", COMPOSE.read_text()))
    assert len(defaults) == 1, f"{var}: {defaults}"
    return defaults.pop()


def test_env_example_uses_the_dev_credentials():
    env = env_example()
    url = make_url(env["DATABASE_URL"])
    assert (url.username, url.password) == PG
    assert (env["POSTGRES_USER"], env["POSTGRES_PASSWORD"]) == PG
    assert url.database == "innovision_analytics"
    assert (env["MINIO_ACCESS_KEY"], env["MINIO_SECRET_KEY"]) == MINIO


def test_compose_defaults_match_env_example():
    """Before: compose defaulted MinIO to admin/password123 and .env.example
    used postgres:postgres in DATABASE_URL."""
    assert (compose_default("POSTGRES_USER"), compose_default("POSTGRES_PASSWORD")) == PG
    assert (compose_default("MINIO_ACCESS_KEY"), compose_default("MINIO_SECRET_KEY")) == MINIO


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_compose_resolves_the_same_credentials_with_env_example():
    result = subprocess.run(
        ["docker", "compose", "--env-file", ".env.example", "-f", str(COMPOSE), "config"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0 and "compose" in result.stderr and "unknown" in result.stderr:
        pytest.skip("docker compose plugin not installed")
    assert result.returncode == 0, result.stderr
    cfg = result.stdout
    assert "POSTGRES_USER: analytics" in cfg and "POSTGRES_PASSWORD: analytics" in cfg
    assert cfg.count("MINIO_ROOT_USER: minioadmin") == 2  # minio + minio-init
    assert cfg.count("MINIO_ROOT_PASSWORD: minioadmin") == 2


def test_makefile_passes_env_file_to_compose():
    makefile = (ROOT / "Makefile").read_text()
    compose_lines = [line for line in makefile.splitlines() if "docker compose" in line]
    assert compose_lines and all("--env-file .env" in line for line in compose_lines)
    assert re.search(r"^infra: \.env$", makefile, re.M)


def test_code_default_database_urls_use_analytics_credentials(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from services.detection.src.config import DetectionSettings
    from services.event_processing.src.workers.headcount.config import HeadcountConfig
    from services.event_processing.src.workers.intruder.config import IntruderConfig
    from services.event_processing.src.workers.zone_monitor.config import ZoneMonitorConfig
    from shared.config import Settings

    urls = [
        Settings().DATABASE_URL,
        DetectionSettings().database_url,
        HeadcountConfig().DATABASE_URL,
        IntruderConfig().DATABASE_URL,
        ZoneMonitorConfig().DATABASE_URL,
    ]
    for url in urls:
        parsed = make_url(url)
        assert (parsed.username, parsed.password, parsed.database) == (*PG, "innovision_analytics")
