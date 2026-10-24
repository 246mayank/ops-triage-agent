"""Deterministic scoring: exact category match and string-matched evidence. No LLM judge."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from triage.schema import Diagnosis


@dataclass
class RequiredEvidence:
    id: str
    match: str


@dataclass
class Expected:
    incident: str
    split: str
    status: str
    category: str | None
    required_evidence: list[RequiredEvidence]
    min_evidence_hits: int
    fix_hint: str = ""

    @classmethod
    def load(cls, incident_dir: Path) -> Expected:
        d = yaml.safe_load((Path(incident_dir) / "expected.yaml").read_text(encoding="utf-8"))
        d["required_evidence"] = [RequiredEvidence(**r) for r in d.get("required_evidence") or []]
        return cls(**d)


@dataclass
class Score:
    status_ok: bool
    category_ok: bool
    hits: list[str]
    strict: bool
    ungrounded: int
    cost: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def score(diag: Diagnosis | None, expected: Expected, ungrounded: int,
          cost: dict | None = None) -> Score:
    """ungrounded: quotes in the final diagnosis that do not appear in their cited result."""
    if diag is None:
        return Score(False, False, [], False, ungrounded, cost or {})
    status_ok = diag.status == expected.status
    cat_ok = status_ok and (expected.status != "diagnosed"
                            or (diag.category is not None
                                and diag.category.value == expected.category))
    quotes = " \n ".join(e.quote.lower() for e in diag.evidence)
    hits = [r.id for r in expected.required_evidence if r.match.lower() in quotes]
    strict = cat_ok and len(hits) >= expected.min_evidence_hits and ungrounded == 0
    return Score(status_ok, cat_ok, hits, strict, ungrounded, cost or {})
