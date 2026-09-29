"""tools/platform/common.py: masking, .env parsing, registry client, compose parsing."""
import re

import pytest

from tests.tools import fakes
from tests.tools.registry_stub import ADMIN, SERVICE_TOKEN
from tools.platform.common import (
    ROOT, RegistryError, Reporter, compose_services, find_service, mask_env_text,
    mask_text, mask_url, read_env_file,
)

# ---------------------------------------------------------------- masking


@pytest.mark.parametrize("url, expected", [
    (fakes.userinfo_url("rtsp", "10.0.0.9:554", "/stream1", user="admin", label="cam"),
     "rtsp://***@10.0.0.9:554/stream1"),
    ("http://10.0.0.5:4747/video", "http://10.0.0.5:4747/video"),
    ("http://cam/video?user=bob&" + fakes.assignment("password", "query") + "&res=hd",
     "http://cam/video?user=***&password=***&res=hd"),
    ("http://cam/video?" + fakes.assignment("token", "query"), "http://cam/video?token=***"),
    (fakes.userinfo_url("postgresql+asyncpg", "localhost:5432", "/db", user="u", label="db"),
     "postgresql+asyncpg://***@localhost:5432/db"),
])
def test_mask_url(url, expected):
    assert mask_url(url) == expected
    assert fakes.MARKER not in mask_url(url)


def test_mask_text_hides_urls_bearer_and_key_values():
    text = ("ffmpeg: " + fakes.userinfo_url("rtsp", "10.0.0.9", "/s", user="admin", label="cam")
            + " failed; Authorization: Bearer " + fakes.fake("jwt") + ".x.y "
            + fakes.assignment("INTERNAL_SERVICE_TOKEN", "token") + " "
            + fakes.assignment("MINIO_SECRET_KEY", "minio", sep=": "))
    assert text.count(fakes.MARKER) == 4
    out = mask_text(text)
    assert fakes.MARKER not in out and "admin:" not in out
    assert "10.0.0.9" in out
    assert "rtsp://***@10.0.0.9/s" in out and "Bearer ***" in out
    assert "INTERNAL_SERVICE_TOKEN=***" in out and "MINIO_SECRET_KEY: ***" in out


def test_mask_env_text_masks_every_secret_keeps_the_rest():
    lines = [
        "# comment with " + fakes.userinfo_url("rtsp", "cam", "/x", user="u", label="comment"),
        "REDIS_HOST=localhost",
        fakes.assignment("INTERNAL_SERVICE_TOKEN", "token"),
        fakes.assignment("MINIO_ACCESS_KEY", "access"),
        fakes.assignment("MINIO_SECRET_KEY", "secret"),
        "POSTGRES_PASSWORD='" + fakes.fake("pg") + "'",
        "DATABASE_URL=" + fakes.userinfo_url("postgresql+asyncpg", "localhost:5432",
                                             "/innovision_analytics", label="db"),
        fakes.assignment("JWT_SECRET", "jwt"),
        fakes.assignment("AUTH_HEADER", "auth"),
        "EMPTY_TOKEN=",
    ]
    env = "\n".join(lines) + "\n"
    assert env.count(fakes.MARKER) == 8
    out = mask_env_text(env)
    assert fakes.MARKER not in out
    assert "://u:" not in out and "://user:" not in out
    for key in ("INTERNAL_SERVICE_TOKEN", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD",
                "JWT_SECRET", "AUTH_HEADER"):
        assert f"{key}=***" in out, key
    assert "rtsp://***@cam/x" in out
    assert "REDIS_HOST=localhost" in out
    assert "localhost:5432/innovision_analytics" in out
    assert "EMPTY_TOKEN=" in out  # empty stays visibly empty (diagnoses a missing token)


def test_read_env_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text('A=1\n# B=2\nC="x y"\nD=z  # trailing\nexport E=5\nbad line\n', encoding="utf-8")
    assert read_env_file(p) == {"A": "1", "C": "x y", "D": "z", "E": "5"}


# ---------------------------------------------------------------- reporter


def test_reporter_fail_prints_fix_and_exit_code(capsys):
    r = Reporter()
    r.ok("redis", "reachable")
    r.fail("minio", "unreachable", "start the platform stack")
    assert r.summary() == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "PASS redis: reachable"
    assert out[1] == "FAIL minio: unreachable"
    assert out[2].strip() == "FIX: start the platform stack"
    assert out[-1].startswith("RESULT: FAIL")


# ---------------------------------------------------------------- registry client


def test_resolve_by_name_and_unknown_lists_names(registry):
    cam = registry.add("Phone 1")
    registry.add("Phone 2")
    c = registry.client()
    assert c.resolve("Phone 1").id == cam["id"]
    with pytest.raises(RegistryError) as exc:
        c.resolve("Phone 9")
    assert "'Phone 1'" in str(exc.value) and "'Phone 2'" in str(exc.value)


def test_resolve_duplicate_name_fails(registry):
    registry.add("Phone 1")
    registry.add("Phone 1")
    with pytest.raises(RegistryError, match="used by 2 cameras"):
        registry.client().resolve("Phone 1")


def test_wrong_service_token_is_explained(registry):
    with pytest.raises(RegistryError) as exc:
        registry.client(token="wrong").active_cameras()
    assert "INTERNAL_SERVICE_TOKEN" in exc.value.fix


def test_empty_service_token_fails_before_calling(registry):
    with pytest.raises(RegistryError, match="empty"):
        registry.client(token="").active_cameras()
    assert registry.calls == []


def test_login_create_update_and_by_uc(registry):
    c = registry.client()
    with pytest.raises(RegistryError, match="login failed"):
        c.login(ADMIN[0], "wrong")
    c.login(*ADMIN)
    cam = c.create_camera(name="Phone 1", location="Lobby", rtsp_url="http://1.2.3.4:4747/video",
                          use_cases=["uc1"], fps=5)
    assert c.by_uc("uc1") == [cam.id]
    c.update_camera(cam.id, {"fps": 10})
    assert registry.cameras[cam.id]["fps"] == 10


def test_unreachable_registry_has_fix():
    from tools.platform.common import RegistryClient

    with pytest.raises(RegistryError) as exc:
        RegistryClient("http://127.0.0.1:1", timeout=1).by_uc("uc1")
    assert "docker-compose.yml" in exc.value.fix


# ---------------------------------------------------------------- compose


def test_compose_services_and_find(tmp_path):
    f = tmp_path / "docker-compose.yml"
    f.write_text("services:\n  alert-management:\n    container_name: innovision-alerts\n"
                 "  camera_registry: {}\n", encoding="utf-8")
    svcs = compose_services(f)
    assert svcs == {"alert-management": "innovision-alerts", "camera_registry": None}
    assert find_service(svcs, "alert", "manag") == "alert-management"
    assert find_service(svcs, "nothing") is None


# ---------------------------------------------------------------- .env.platform.example

TOOL_ONLY = {"PLATFORM_AUTH_URL", "INTERNAL_SERVICE_TOKEN", "PLATFORM_DATABASE_URL"}


def test_env_platform_example_keys_are_real_settings_with_comments():
    example = ROOT / ".env.platform.example"
    known = set(read_env_file(ROOT / ".env.example")) | TOOL_ONLY
    values = read_env_file(example)
    assert set(values) - known == set()
    assert {"REDIS_DB", "MINIO_ENDPOINT", "CAMERA_REGISTRY_URL", "SOURCE_UC", "DATABASE_URL",
            "YOLOV11M_PATH", "MODEL_ROOT", "RECOGNITION_MODEL_PACK", "DETECTION_IMGSZ"} | TOOL_ONLY <= set(values)
    assert values["REDIS_DB"] == "0"
    assert values["DATABASE_URL"].endswith("/innovision_analytics")
    lines = example.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):  # every variable has a comment right above it
        if re.match(r"^[A-Z_]+=", line) and line.split("=")[0] != "LOG_LEVEL":
            assert lines[i - 1].startswith("#"), line


def test_env_platform_example_holds_placeholders_only():
    values = read_env_file(ROOT / ".env.platform.example")
    for key in ("MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "INTERNAL_SERVICE_TOKEN"):
        assert values[key].startswith("<"), key
    for key in ("DATABASE_URL", "PLATFORM_DATABASE_URL"):
        assert "<user>:<password>@" in values[key]
    assert SERVICE_TOKEN not in (ROOT / ".env.platform.example").read_text()
