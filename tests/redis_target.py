"""Where tests may use Redis. Tests must NEVER touch the platform's data:
every test client and every service under test uses the dedicated database
TEST_REDIS_DB (default 15, never 0) and only that database is flushed.

  TEST_REDIS_URL  host/port of the shared test Redis (default redis://127.0.0.1:6379);
                  a database path, if given, must equal TEST_REDIS_DB
  TEST_REDIS_DB   dedicated database index (default 15; 0 is refused)
  REQUIRE_REAL_SERVICES=1  unreachable Redis/Postgres FAILS instead of skipping (CI)
"""
import os
from urllib.parse import urlparse

import pytest

TEST_REDIS_DB = int(os.environ.get("TEST_REDIS_DB", "15"))

_url = urlparse(os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379"))
HOST = _url.hostname or "127.0.0.1"
PORT = _url.port or 6379
_URL_DB = _url.path.strip("/") or None


def check_target(db: int = TEST_REDIS_DB, url_db: str | None = _URL_DB) -> str | None:
    """Why the configured test database is not allowed, or None if it is."""
    if db == 0:
        return (
            "TEST_REDIS_DB=0 is the platform's Redis database; tests refuse to "
            "run against it. Use a dedicated index (default 15)."
        )
    if url_db is not None and int(url_db) != db:
        return (
            f"TEST_REDIS_URL names database {url_db} but TEST_REDIS_DB is {db}; "
            "they must agree (or leave the database out of the URL)."
        )
    return None


def url(host: str = HOST, port: int = PORT) -> str:
    return f"redis://{host}:{port}/{TEST_REDIS_DB}"


def unavailable(reason: str):
    """Skip locally; fail when REQUIRE_REAL_SERVICES=1 so CI can't hide it."""
    if os.environ.get("REQUIRE_REAL_SERVICES") == "1":
        pytest.fail(f"{reason} (REQUIRE_REAL_SERVICES=1)")
    pytest.skip(reason)


def point_services_at_test_redis(monkeypatch, host: str = HOST, port: int = PORT) -> None:
    """Make every service config under test connect to the test database."""
    from services.event_processing.src.workers.headcount.config import config as hc
    from services.event_processing.src.workers.intruder.config import config as ic
    from services.event_processing.src.workers.zone_monitor.config import config as zm
    from shared.config import settings

    for cfg in (settings, hc, ic, zm):
        monkeypatch.setattr(cfg, "REDIS_HOST", host)
        monkeypatch.setattr(cfg, "REDIS_PORT", port)
        monkeypatch.setattr(cfg, "REDIS_DB", TEST_REDIS_DB)
