import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INCIDENTS = ROOT / "incidents"
sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(scope="session", autouse=True)
def built_incidents():
    """world.db is not committed; build any incident that is missing it."""
    from sandbox.build import build_incident, incident_dirs

    for d in incident_dirs():
        if not (d / "world.db").exists():
            build_incident(d)


@pytest.fixture
def inc01() -> Path:
    return INCIDENTS / "01_silent_partial_load"


@pytest.fixture
def reg01(inc01):
    from triage.tools.registry import default_registry

    return default_registry(inc01)
