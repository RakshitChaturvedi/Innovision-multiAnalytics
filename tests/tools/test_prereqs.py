"""tools/platform/prereqs.py: env comparisons, stubs detection, models, tools, services."""
from pathlib import Path

from tests import redis_target
from tests.tools import fakes
from tests.tools.registry_stub import SERVICE_TOKEN
from tools.platform import prereqs
from tools.platform.common import Reporter


def _platform(tmp_path, stubs_yaml=None, main_yaml="services:\n  alert_management: {}\n"):
    infra = tmp_path / "platform" / "infra"
    infra.mkdir(parents=True)
    (infra / "docker-compose.yml").write_text(main_yaml, encoding="utf-8")
    if stubs_yaml is not None:
        (infra / "docker-compose.stubs.yml").write_text(stubs_yaml, encoding="utf-8")
    return tmp_path / "platform"


def _docker_ps(rows):
    def runner(cmd, timeout=20):
        assert cmd[:2] == ["docker", "ps"]
        return 0, "\n".join("|".join(r) for r in rows) + "\n"
    return runner


# ---------------------------------------------------------------- stubs


def test_stub_container_detected_by_compose_label(tmp_path, capsys):
    p = _platform(tmp_path, "services:\n  fake_ingestion: {}\n")
    runner = _docker_ps([
        ("platform-ingestion-1", "ingestion", r"C:\p\infra\docker-compose.yml"),
        ("platform-fake_ingestion-1", "fake_ingestion", r"C:\p\infra\docker-compose.stubs.yml"),
    ])
    r = Reporter()
    prereqs.check_stubs(r, p, runner)
    assert r.failed == 1
    out = capsys.readouterr().out
    assert "platform-fake_ingestion-1" in out and "platform-ingestion-1" not in out
    assert "docker-compose.stubs.yml down" in out


def test_stub_container_detected_by_stub_only_container_name(tmp_path):
    p = _platform(tmp_path, "services:\n  x:\n    container_name: stub-alerts\n")
    runner = _docker_ps([("stub-alerts", "x", "")])
    assert prereqs.running_stub_containers(p, runner) == ["stub-alerts"]


def test_real_service_sharing_a_name_with_a_stub_is_not_flagged(tmp_path):
    shared = "services:\n  ingestion:\n    container_name: innovision-ingestion\n"
    p = _platform(tmp_path, shared, main_yaml=shared)
    runner = _docker_ps([("innovision-ingestion", "ingestion", "/p/infra/docker-compose.yml")])
    r = Reporter()
    prereqs.check_stubs(r, p, runner)
    assert r.failed == 0


def test_docker_down_is_a_fail_with_fix(tmp_path, capsys):
    r = Reporter()
    prereqs.check_stubs(r, _platform(tmp_path), lambda cmd, timeout=20: (1, "error"))
    assert r.failed == 1 and "Docker Desktop" in capsys.readouterr().out


# ---------------------------------------------------------------- .env comparison


def _lines(*pairs) -> str:
    """KEY=<fake> lines (tests/tools/fakes.py)."""
    return "".join(fakes.assignment(key, label) + "\n" for key, label in pairs)


def _envs(tmp_path, mine: str, plat: str):
    a, b = tmp_path / "a.env", tmp_path / "b.env"
    a.write_text(mine, encoding="utf-8")
    b.write_text(plat, encoding="utf-8")
    return a, b


def test_env_files_equal_pass_and_never_print_values(tmp_path, capsys):
    a, b = _envs(tmp_path,
                 _lines(("INTERNAL_SERVICE_TOKEN", "token"), ("MINIO_ACCESS_KEY", "access"),
                        ("MINIO_SECRET_KEY", "secret")),
                 _lines(("INTERNAL_SERVICE_TOKEN", "token"), ("MINIO_ROOT_USER", "access"),
                        ("MINIO_ROOT_PASSWORD", "secret")))
    r = Reporter()
    prereqs.check_env_files(r, a, b)
    out = capsys.readouterr().out
    assert r.failed == 0, out
    assert fakes.MARKER not in out


def test_env_token_missing_in_platform_explains_compose(tmp_path, capsys):
    a, b = _envs(tmp_path, _lines(("INTERNAL_SERVICE_TOKEN", "token"), ("MINIO_ACCESS_KEY", "access"),
                                  ("MINIO_SECRET_KEY", "secret")),
                 _lines(("MINIO_ACCESS_KEY", "access"), ("MINIO_SECRET_KEY", "secret")))
    r = Reporter()
    prereqs.check_env_files(r, a, b)
    out = capsys.readouterr().out
    assert r.failed == 1 and "docker-compose.yml" in out


def test_env_mismatches_fail_without_printing_values(tmp_path, capsys):
    a, b = _envs(tmp_path, _lines(("INTERNAL_SERVICE_TOKEN", "token-a"), ("MINIO_ACCESS_KEY", "access-a"),
                                  ("MINIO_SECRET_KEY", "secret-a")),
                 _lines(("INTERNAL_SERVICE_TOKEN", "token-b"), ("MINIO_ACCESS_KEY", "access-b"),
                        ("MINIO_SECRET_KEY", "secret-b")))
    r = Reporter()
    prereqs.check_env_files(r, a, b)
    out = capsys.readouterr().out
    assert r.failed == 3
    assert fakes.MARKER not in out


def test_missing_env_file_fails(tmp_path):
    r = Reporter()
    prereqs.check_env_files(r, tmp_path / "nope.env", tmp_path / "nope2.env")
    assert r.failed == 2


# ---------------------------------------------------------------- models, tools


def test_models_present_and_missing(tmp_path):
    w = tmp_path / "yolo.pt"
    w.write_bytes(b"x")
    pack = tmp_path / "rec" / "models" / "buffalo_s"
    pack.mkdir(parents=True)
    (pack / "w600k.onnx").write_bytes(b"x")
    r = Reporter()
    prereqs.check_models(r, {"YOLOV11M_PATH": str(w), "MODEL_ROOT": str(tmp_path / "rec"),
                             "RECOGNITION_MODEL_PACK": "buffalo_s"})
    assert r.failed == 0
    r = Reporter()
    prereqs.check_models(r, {"YOLOV11M_PATH": str(tmp_path / "no.pt"), "MODEL_ROOT": str(tmp_path)})
    assert r.failed == 2


def test_tools_python_version_and_missing_packages(capsys):
    r = Reporter()
    prereqs.check_tools(r, runner=lambda cmd, timeout=20: (0, "27.0"), version=(3, 12, 7),
                        find_spec=lambda m: object(), which=lambda e: f"/bin/{e}")
    assert r.failed == 0
    r = Reporter()
    prereqs.check_tools(r, runner=lambda cmd, timeout=20: (1, ""), version=(3, 13, 0),
                        find_spec=lambda m: None if m == "insightface" else object(),
                        which=lambda e: None)
    out = capsys.readouterr().out
    assert r.failed == 5  # docker, python, venv, ffmpeg, ffprobe
    assert "insightface" in out and "py -3.12" in out


# ---------------------------------------------------------------- services (real Redis)


def test_services_reachable_against_real_redis_and_stub_registry(redis_client, registry, capsys):
    registry.add("Phone 1")
    env = {"REDIS_HOST": redis_target.HOST, "REDIS_PORT": str(redis_target.PORT),
           "REDIS_DB": str(redis_target.TEST_REDIS_DB),
           "DATABASE_URL": fakes.userinfo_url("postgresql+asyncpg", "127.0.0.1:1", "/x", user="u",
                                               label="db"),   # closed port
           "MINIO_ENDPOINT": "127.0.0.1:1",
           "PLATFORM_AUTH_URL": registry.url, "CAMERA_REGISTRY_URL": registry.url,
           "INTERNAL_SERVICE_TOKEN": SERVICE_TOKEN}
    r = Reporter()
    client = prereqs.check_services(r, env)
    out = capsys.readouterr().out
    assert client is not None
    assert "PASS redis" in out and "PASS registry" in out and "1 camera(s) for uc1" in out
    assert "FAIL postgres" in out and "FAIL minio" in out
    assert r.failed == 2
    assert fakes.MARKER not in out


def test_main_without_platform_compose_fails(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(prereqs, "check_tools", lambda r: None)
    monkeypatch.setattr(prereqs, "check_git", lambda *a: None)
    monkeypatch.setattr(prereqs, "check_services", lambda r, env: None)
    monkeypatch.setattr(prereqs, "check_stubs", lambda r, p: None)
    monkeypatch.setattr(prereqs, "ROOT", Path(tmp_path))
    assert prereqs.main(["--platform-path", str(tmp_path / "nope")]) == 1
    assert "-PlatformPath" in capsys.readouterr().out


def test_warns_when_test_cameras_subscribed_to_uc1(registry, capsys):
    registry.add("Phone 1")
    registry.add("Test Camera UC1")
    registry.add("Test Camera UC2", use_cases=())   # already unsubscribed: not listed
    r = Reporter()
    prereqs.check_test_cameras(r, registry.client(), registry.client().by_uc("uc1"), "uc1")
    out = capsys.readouterr().out
    assert r.warned == 1 and r.failed == 0
    assert "WARN test cameras" in out and "Test Camera UC1" in out and "UC2" not in out
    assert "unsubscribe_test_cameras.ps1" in out


def test_no_warning_without_test_cameras(registry, capsys):
    registry.add("Phone 1")
    r = Reporter()
    prereqs.check_test_cameras(r, registry.client(), registry.client().by_uc("uc1"), "uc1")
    assert r.warned == 0 and "PASS test cameras" in capsys.readouterr().out
