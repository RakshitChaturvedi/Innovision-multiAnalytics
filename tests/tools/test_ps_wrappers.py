"""Smoke tests of the scripts/*.ps1 thin wrappers with real PowerShell (pwsh).
Skipped when pwsh is not installed. Redis: the test database only (never 0)."""
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from tests import redis_target
from tests.tools import fakes
from tests.tools.registry_stub import ADMIN, SERVICE_TOKEN
from tools.platform.common import ROOT

PWSH = shutil.which("pwsh") or ("/opt/pwsh/pwsh" if Path("/opt/pwsh/pwsh").exists() else None)
pytestmark = pytest.mark.skipif(PWSH is None, reason="pwsh not installed")


def ps(script, *args, env=None, stdin=None, timeout=120):
    e = {**os.environ, "REDIS_HOST": redis_target.HOST, "REDIS_PORT": str(redis_target.PORT),
         "REDIS_DB": str(redis_target.TEST_REDIS_DB), **(env or {})}
    p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File",
                        str(ROOT / "scripts" / script), *args],
                       capture_output=True, text=True, env=e, input=stdin, timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def test_check_prereqs_forwards_platform_path_and_exit_code(tmp_path):
    code, out = ps("check_prereqs.ps1", "-PlatformPath", str(tmp_path / "nope"))
    assert code == 1
    assert "FAIL platform path" in out and "FIX:" in out and "RESULT: FAIL" in out


def test_check_prereqs_requires_platform_path():
    code, out = ps("check_prereqs.ps1")
    assert code != 0 and "PlatformPath" in out


def test_test_camera_url_masks_and_fails(tmp_path):
    code, out = ps("test_camera_url.ps1", "-Url", fakes.userinfo_url("rtsp", "127.0.0.1:1", "/x", label="ps"),
                   "-Out", str(tmp_path / "f.jpg"))
    assert code == 1 and "FAIL frame" in out and fakes.MARKER not in out


def test_register_cameras_create_and_update_paths(registry):
    env = {"CAMERA_REGISTRY_URL": registry.url, "PLATFORM_AUTH_URL": registry.url,
           "INTERNAL_SERVICE_TOKEN": SERVICE_TOKEN, "SOURCE_UC": "uc1"}
    creds = f"{ADMIN[0]}\n{ADMIN[1]}\n"
    code, out = ps("register_cameras.ps1", "-Name", "Phone 1", "-Location", "Lobby",
                   "-Url", "http://10.0.0.5:4747/video", "-Fps", "5", env=env, stdin=creds)
    assert code == 0, out
    assert "PASS camera: 'Phone 1' created" in out and "PASS discovery" in out
    assert ADMIN[1] not in out

    code, out = ps("register_cameras.ps1", "-Name", "Phone 1", "-Location", "Lobby",
                   "-Url", "http://10.0.0.6:4747/video", "-Fps", "10", env=env)
    assert code == 0 and "-Update" in out and "fps 5 -> 10" in out
    assert [c["fps"] for c in registry.cameras.values()] == [5]

    code, out = ps("register_cameras.ps1", "-Name", "Phone 1", "-Location", "Lobby",
                   "-Url", "http://10.0.0.6:4747/video", "-Fps", "10", "-Update", env=env, stdin=creds)
    assert code == 0, out
    assert [c["fps"] for c in registry.cameras.values()] == [10]


def test_platform_db_refuses_platform_database():
    code, out = ps("platform_db.ps1",
                   env={"DATABASE_URL": fakes.userinfo_url("postgresql+asyncpg", "127.0.0.1:1",
                                                         "/innovision_platform", user="u", label="db")})
    assert code == 1 and "innovision_analytics" in out and fakes.MARKER not in out


def test_up_refuses_on_failed_preflight_and_down_is_clean():
    env = {"CAMERA_REGISTRY_URL": "http://127.0.0.1:1", "INTERNAL_SERVICE_TOKEN": "",
           "MINIO_ENDPOINT": "127.0.0.1:1"}
    code, out = ps("up.ps1", "-Wait", "1", env=env)
    assert code == 1 and "Not starting" in out
    code, out = ps("down.ps1", "-Timeout", "1")
    assert code == 0 and "RESULT: PASS" in out


def test_collect_diagnostics_writes_masked_zip(tmp_path):
    before = set(ROOT.glob("diagnostics_*.zip"))
    code, out = ps("collect_diagnostics.ps1", env={"CAMERA_REGISTRY_URL": "http://127.0.0.1:1"},
                   timeout=300)
    new = set(ROOT.glob("diagnostics_*.zip")) - before
    try:
        assert code == 0, out
        assert len(new) == 1
        with zipfile.ZipFile(next(iter(new))) as z:
            names = z.namelist()
        assert "check_integration.txt" in names and "streams.txt" in names
        assert not any(n.endswith((".png", ".jpg", ".onnx", ".pt")) for n in names)
    finally:
        for f in new:
            f.unlink()
