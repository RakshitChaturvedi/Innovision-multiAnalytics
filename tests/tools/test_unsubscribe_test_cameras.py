from tests.tools.registry_stub import ADMIN
from tools.platform import unsubscribe_test_cameras as unsub
from tools.platform.common import Reporter


def _seed(registry):
    t1 = registry.add("Test Camera UC1", use_cases=("uc1",), rtsp_url="/videos/uc1.mp4")
    t2 = registry.add("Test Camera UC2", use_cases=("uc1", "uc2"), rtsp_url="/videos/uc2.mp4")
    t3 = registry.add("Test Camera UC3", use_cases=())
    phone = registry.add("Phone 1", use_cases=("uc1",))
    return t1, t2, t3, phone


def test_clears_use_cases_of_test_cameras_only_and_prints_changes(registry, capsys):
    t1, t2, t3, phone = _seed(registry)
    r = Reporter()
    changed = unsub.unsubscribe(registry.client(), r, dry_run=False, credentials=lambda: ADMIN)
    out = capsys.readouterr().out
    assert {c.id for c in changed} == {t1["id"], t2["id"]}
    assert registry.cameras[t1["id"]]["use_cases"] == []
    assert registry.cameras[t2["id"]]["use_cases"] == []
    assert registry.cameras[phone["id"]]["use_cases"] == ["uc1"]
    assert f"PASS Test Camera UC2: {t2['id']} use_cases ['uc1', 'uc2'] -> []" in out
    assert f"{t3['id']} use_cases [] (unchanged)" in out
    assert "Phone 1" not in out and r.failed == 0
    puts = [c for c in registry.calls if c[0] == "PUT"]
    assert sorted(puts) == sorted([("PUT", f"/cameras/{t1['id']}/config"),
                                   ("PUT", f"/cameras/{t2['id']}/config")])


def test_dry_run_sends_nothing_and_needs_no_login(registry, capsys):
    t1, *_ = _seed(registry)

    def no_login():
        raise AssertionError("dry run must not log in")

    changed = unsub.unsubscribe(registry.client(), Reporter(), dry_run=True, credentials=no_login)
    out = capsys.readouterr().out
    assert len(changed) == 2 and "DRY-RUN Test Camera UC1" in out
    assert registry.cameras[t1["id"]]["use_cases"] == ["uc1"]
    assert not [c for c in registry.calls if c[0] in ("PUT", "POST")]


def test_wrong_login_fails_and_changes_nothing(registry, capsys):
    t1, *_ = _seed(registry)
    r = Reporter()
    assert unsub.unsubscribe(registry.client(), r, dry_run=False,
                             credentials=lambda: (ADMIN[0], "wrong")) == []
    assert r.failed == 1 and registry.cameras[t1["id"]]["use_cases"] == ["uc1"]


def test_no_test_cameras_is_a_pass(registry, capsys):
    registry.add("Phone 1")
    r = Reporter()
    assert unsub.unsubscribe(registry.client(), r, dry_run=False) == []
    assert "PASS test cameras" in capsys.readouterr().out and r.failed == 0
