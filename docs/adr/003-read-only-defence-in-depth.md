# ADR 003: Read-only by defence in depth

**Context.** The agent runs SQL it writes itself against a database, and reads logs written
by systems we do not control (prompt injection is in scope).

**Decision.** No write tools at all, and `run_sql` is read-only through four independent
layers (`src/triage/tools/sql.py`): a `mode=ro` connection; an SQLite authorizer allow-list
(SELECT, READ, FUNCTION, RECURSIVE only, so ATTACH and PRAGMA are denied); a single-statement
check that requires SELECT or WITH; and bounded execution (50 rows, 2 seconds). File tools
resolve paths and refuse anything outside the incident folder.

**Alternatives.** Rely on the prompt; rely on the statement check alone.

**Consequences.** Each layer covers a gap in another: `WITH x AS (SELECT 1) DELETE FROM
orders` passes the statement check but is refused by the engine. A successful injection can
only produce a wrong report, never a change.
