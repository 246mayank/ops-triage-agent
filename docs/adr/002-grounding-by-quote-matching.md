# ADR 002: Grounding by quote matching instead of an LLM judge

**Context.** A model can write a confident, specific, wrong root cause. The eval and the
production path both need a check that cannot drift.

**Decision.** Every evidence item names the tool result it came from and quotes it; code
checks (case- and whitespace-normalised) that the quote appears in that result's text as the
model saw it (`src/triage/validate.py`). One repair turn, then downgrade to
`insufficient_evidence`. Scoring uses exact category match plus required evidence strings.

**Alternatives.** An LLM judge grading the diagnosis; embedding similarity.

**Consequences.** Deterministic, cheap and impossible to argue with. Limit: it proves the
evidence exists, not that the reasoning from it is right. The exact category match covers
part of that gap.
