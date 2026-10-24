"""Generates incidents/NN_*/ from each spec.yaml.

world.db is not committed (it is a few MB per incident). Instead, incidents/CHECKSUMS.txt
records a content hash of every generated file, and `--check` rebuilds everything into a
temporary folder and fails if any hash differs.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path

import yaml

from sandbox.faults import FAULTS, FIRST_DAY_OVERRIDE
from sandbox.world import FIRST_DAY, build_world, db_digest, write_world

ROOT = Path(__file__).resolve().parent.parent
INCIDENTS = ROOT / "incidents"
GENERATED = ("world.db", "alert.json", "logs", "config")


def incident_dirs(root: Path = INCIDENTS) -> list[Path]:
    return sorted(p for p in root.iterdir() if (p / "spec.yaml").exists())


def build_incident(spec_dir: Path, out_dir: Path | None = None) -> Path:
    spec = yaml.safe_load((spec_dir / "spec.yaml").read_text(encoding="utf-8"))
    out = out_dir or spec_dir
    for name in GENERATED:
        target = out / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    fault = spec["fault"]
    world = build_world(spec["seed"], FIRST_DAY_OVERRIDE.get(fault, FIRST_DAY))
    FAULTS[fault](world)
    write_world(world, out)
    return out


def digests(incident_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for f in sorted(incident_dir.rglob("*")):
        if not f.is_file() or f.name in ("spec.yaml", "expected.yaml", "oracle.yaml"):
            continue
        rel = f.relative_to(incident_dir).as_posix()
        if f.name == "world.db":
            out[rel] = db_digest(f)
        else:
            out[rel] = hashlib.sha256(f.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    return out


def render_checksums(all_digests: dict[str, dict[str, str]]) -> str:
    lines = []
    for inc, ds in sorted(all_digests.items()):
        for rel, h in ds.items():
            lines.append(f"{h}  {inc}/{rel}")
    return "\n".join(lines) + "\n"


def build_all(check: bool = False, root: Path = INCIDENTS) -> tuple[bool, list[str]]:
    """Build every incident. With check=True, build into a temp dir and compare hashes.
    Returns (ok, list of differing paths)."""
    dirs = incident_dirs(root)
    sums_file = root / "CHECKSUMS.txt"
    if not check:
        all_d = {d.name: digests(build_incident(d)) for d in dirs}
        sums_file.write_text(render_checksums(all_d), encoding="utf-8", newline="\n")
        return True, []
    with tempfile.TemporaryDirectory() as tmp:
        all_d = {d.name: digests(build_incident(d, Path(tmp) / d.name)) for d in dirs}
    fresh = render_checksums(all_d).splitlines()
    old = sums_file.read_text(encoding="utf-8").splitlines() if sums_file.exists() else []
    diff = sorted(set(fresh) ^ set(old))
    return not diff, [ln.split("  ", 1)[1] for ln in diff]
