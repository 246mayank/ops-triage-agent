import pytest

ESCAPES = ["../world.db", "../../sandbox/world.py", "history/../../alert.json",
           "C:/Windows/win.ini", "/etc/passwd", "..\\spec.yaml"]


@pytest.mark.parametrize("path", ESCAPES)
def test_read_config_refuses_escape(reg01, path):
    res = reg01.run("read_config", {"path": path})
    assert not res.ok
    assert res.data == ""


def test_read_config_ok(reg01):
    res = reg01.run("read_config", {"path": "pipeline.yaml"})
    assert res.ok and "schedule:" in res.data


def test_config_history_refuses_escape(reg01):
    assert not reg01.run("config_history", {"path": "../../alert.json"}).ok


def test_config_history_lists_versions_with_diffs(reg01):
    res = reg01.run("config_history", {"path": "pipeline.yaml"})
    assert res.ok and "2026-09-29T11:05:00Z" in res.data and "+  batch_size: 500" in res.data


def test_search_logs_refuses_unknown_service(reg01):
    assert not reg01.run("search_logs", {"service": "../world"}).ok
