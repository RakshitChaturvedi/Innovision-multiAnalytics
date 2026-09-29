"""tools/platform/register_camera.py against the registry stub."""
from tests.tools import fakes
from tests.tools.registry_stub import ADMIN, SERVICE_TOKEN
from tools.platform import register_camera as rc
from tools.platform.common import Reporter

URL = fakes.userinfo_url("rtsp", "10.0.0.7:554", "/live", user="cam", label="cam-url")


def creds():
    return ADMIN


def no_creds():
    raise AssertionError("credentials must not be asked for")


def _register(registry, **kw):
    args = dict(name="Phone 1", location="Lobby", url=URL, fps=5, uc="uc1",
                credentials=creds, verify_s=0)
    args.update(kw)
    r = Reporter()
    return r, rc.register(registry.client(), r, **args)


def test_new_camera_is_created_and_verified(registry, capsys):
    r, cam_id = _register(registry)
    out = capsys.readouterr().out
    assert r.failed == 0, out
    cam = registry.cameras[cam_id]
    assert cam["use_cases"] == ["uc1"] and cam["fps"] == 5 and cam["rtsp_url"] == URL
    assert "PASS discovery" in out
    assert fakes.MARKER not in out


def test_existing_identical_camera_needs_no_login(registry, capsys):
    cam = registry.add("Phone 1", rtsp_url=URL, fps=5)
    r, cam_id = _register(registry, credentials=no_creds)
    assert cam_id == cam["id"] and r.failed == 0 and r.warned == 0
    assert ("POST", "/cameras") not in registry.calls


def test_existing_different_camera_is_only_reported_without_update(registry, capsys):
    cam = registry.add("Phone 1", use_cases=["uc3"], rtsp_url=fakes.userinfo_url("http", "10.0.0.5", "/video", user="u", label="old-url"), fps=10)
    r, cam_id = _register(registry, credentials=no_creds)
    out = capsys.readouterr().out
    assert cam_id == cam["id"]
    assert "-Update" in out
    assert "fps 10 -> 5" in out and "use_cases ['uc3'] -> ['uc3', 'uc1']" in out
    assert fakes.MARKER not in out
    assert registry.cameras[cam["id"]]["fps"] == 10  # untouched
    assert not any(m == "PUT" for m, _ in registry.calls)
    assert "FAIL discovery" in out  # uc1 missing, and not updated


def test_update_puts_changes_and_keeps_other_use_cases(registry, capsys):
    cam = registry.add("Phone 1", use_cases=["uc3"], rtsp_url="http://10.0.0.5/video", fps=10)
    r, cam_id = _register(registry, update=True)
    out = capsys.readouterr().out
    assert r.failed == 0, out
    stored = registry.cameras[cam["id"]]
    assert stored["use_cases"] == ["uc3", "uc1"] and stored["fps"] == 5 and stored["rtsp_url"] == URL
    assert ("PUT", f"/cameras/{cam['id']}/config") in registry.calls
    assert "PASS discovery" in out


def test_update_with_only_url_change_does_not_touch_use_cases(registry):
    cam = registry.add("Phone 1", use_cases=["uc1", "uc2"], rtsp_url="http://a/video", fps=5)
    assert rc.differences(rc.Camera(cam["id"], "Phone 1", cam), "http://b/video", 5, "uc1") == \
        {"rtsp_url": "http://b/video"}


def test_duplicate_names_fail(registry):
    registry.add("Phone 1")
    registry.add("Phone 1")
    r, cam_id = _register(registry, credentials=no_creds)
    assert cam_id is None and r.failed == 1


def test_bad_login_fails_with_fix(registry, capsys):
    r, cam_id = _register(registry, credentials=lambda: (ADMIN[0], "wrong"))
    out = capsys.readouterr().out
    assert cam_id is None and r.failed == 1 and "FIX:" in out
    assert registry.cameras == {}


def test_restart_command_read_from_compose(tmp_path, capsys):
    infra = tmp_path / "infra"
    infra.mkdir()
    (infra / "docker-compose.yml").write_text(
        "services:\n  camera-registry: {}\n  alert-management-service:\n    image: x\n", encoding="utf-8")
    rc.restart_command(str(tmp_path), Reporter())
    out = capsys.readouterr().out
    assert f"docker compose -f {infra / 'docker-compose.yml'} restart alert-management-service" in out


def test_restart_command_without_platform_path_warns(capsys):
    r = Reporter()
    rc.restart_command(None, r)
    assert r.warned == 1


def test_main_prompts_via_stdin(registry, monkeypatch, capsys):
    import io

    monkeypatch.setenv("CAMERA_REGISTRY_URL", registry.url)
    monkeypatch.setenv("PLATFORM_AUTH_URL", registry.url)
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setenv("SOURCE_UC", "uc1")
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{ADMIN[0]}\n{ADMIN[1]}\n"))
    monkeypatch.setattr(rc, "wait_listed", lambda *a, **k: True)
    assert rc.main(["--name", "Phone 2", "--location", "Hall", "--url", URL]) == 0
    assert [c["name"] for c in registry.cameras.values()] == ["Phone 2"]
