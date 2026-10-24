"""Every incident's hand-written oracle path reaches all of its required evidence, so a low
score is the agent's fault, not the incident's."""

import pytest
import yaml
from conftest import INCIDENTS

from triage.tools.registry import default_registry

DIRS = sorted(d for d in INCIDENTS.iterdir() if (d / "expected.yaml").exists())


def load(p):
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def test_fifteen_incidents_and_test_split():
    splits = [load(d / "expected.yaml")["split"] for d in DIRS]
    assert len(DIRS) == 15 and splits.count("test") == 5


@pytest.mark.parametrize("inc", DIRS, ids=[d.name for d in DIRS])
def test_oracle_path_reaches_required_evidence(inc):
    exp = load(inc / "expected.yaml")
    calls = load(inc / "oracle.yaml")["calls"]
    assert len(calls) <= 6
    reg = default_registry(inc)
    results = [reg.run(c["tool"], c["args"]) for c in calls]
    assert all(r.ok for r in results), [r.note for r in results if not r.ok]
    text = "\n".join(r.render() for r in results).lower()
    missing = [e["match"] for e in exp["required_evidence"] if e["match"].lower() not in text]
    assert not missing


@pytest.mark.parametrize("inc", DIRS, ids=[d.name for d in DIRS])
def test_alert_does_not_leak_cause(inc):
    exp = load(inc / "expected.yaml")
    alert = (inc / "alert.json").read_text(encoding="utf-8").lower()
    for e in exp["required_evidence"]:
        assert e["match"].lower() not in alert, e["id"]
    if exp["category"]:
        assert exp["category"].replace("_", " ") not in alert
