"""shared.config must not require unrelated secrets."""
import importlib

import pytest

REQUIRED_BEFORE = [
    "POSTGRES_USER", "POSTGRES_PASSWORD", "MINIO_ENDPOINT", "MINIO_ACCESS_KEY",
    "MINIO_SECRET_KEY", "MINIO_SECURE", "GRAFANA_PASSWORD", "JWT_SECRET",
    "JWT_ALGORITHM", "JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "JWT_REFRESH_TOKEN_EXPIRE_DAYS",
]


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no stray .env
    for k in REQUIRED_BEFORE + ["REDIS_HOST", "REDIS_PORT", "REDIS_DB", "DATABASE_URL"]:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def load():
    import shared.config as cfg
    return importlib.reload(cfg)


def test_imports_with_no_env_at_all(clean_env):
    cfg = load()
    assert cfg.settings.REDIS_HOST == "redis"
    assert cfg.settings.REDIS_PORT == 6379
    assert "innovision_analytics" in cfg.settings.DATABASE_URL
    assert cfg.settings.redis_url() == "redis://redis:6379/0"


def test_unrelated_env_is_ignored(clean_env):
    for k in REQUIRED_BEFORE:
        clean_env.setenv(k, "x")
    clean_env.setenv("REDIS_HOST", "10.0.0.5")
    clean_env.setenv("REDIS_PORT", "6380")
    cfg = load()
    assert cfg.settings.redis_url() == "redis://10.0.0.5:6380/0"
    assert not hasattr(cfg.settings, "JWT_SECRET")


def test_redis_db_comes_from_env(clean_env):
    clean_env.setenv("REDIS_DB", "15")
    assert load().settings.redis_url() == "redis://redis:6379/15"


def test_base_consumer_uses_redis_url_only(clean_env):
    import inspect
    from shared.schemas.consumer import BaseStreamConsumer
    src = inspect.getsource(BaseStreamConsumer.start)
    assert "redis_url()" in src
    assert "MINIO" not in src and "JWT" not in src
