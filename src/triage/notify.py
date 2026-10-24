"""Chat webhook delivery with alert-id dedupe.

Claim first, post second: an atomic INSERT ... ON CONFLICT DO NOTHING decides which worker
owns an alert, so two workers can never both post. The state database is separate from the
incident database, which the agent can only read.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from triage.schema import Alert, Diagnosis

STATE_DB = Path("state/notify.db")


def _conn(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS posted_alerts (alert_id TEXT PRIMARY KEY, "
                 "run_id TEXT NOT NULL, posted_at TEXT, status TEXT NOT NULL)")
    return conn


def claim(alert_id: str, run_id: str, path: Path = STATE_DB) -> bool:
    """True if this caller now owns the alert; False if someone already claimed it."""
    with _conn(path) as conn:
        cur = conn.execute(
            "INSERT INTO posted_alerts (alert_id, run_id, status) VALUES (?, ?, 'claimed') "
            "ON CONFLICT(alert_id) DO NOTHING", (alert_id, run_id))
        return cur.rowcount == 1


def existing_run(alert_id: str, path: Path = STATE_DB) -> str | None:
    with _conn(path) as conn:
        row = conn.execute("SELECT run_id FROM posted_alerts WHERE alert_id = ?",
                           (alert_id,)).fetchone()
        return row[0] if row else None


def _mark(alert_id: str, status: str, path: Path) -> None:
    with _conn(path) as conn:
        conn.execute("UPDATE posted_alerts SET status = ?, posted_at = ? WHERE alert_id = ?",
                     (status, datetime.now(UTC).isoformat(timespec="seconds"), alert_id))


def format_message(alert: Alert, diag: Diagnosis, run_id: str) -> str:
    lines = [
        f"*Triage for {alert.alert_id}*: {alert.message}",
        f"*{diag.status}*" + (f" · `{diag.category}`" if diag.category else "")
        + f" · confidence {diag.confidence}",
        f"*Root cause:* {diag.root_cause}",
    ]
    for ev in diag.evidence[:3]:
        lines.append(f"> [{ev.tool_call_id}] \"{ev.quote}\"")
    if diag.suggested_fix:
        lines.append(f"*Suggested fix:* {diag.suggested_fix}")
    if diag.next_checks:
        lines.append("*Next checks:* " + "; ".join(diag.next_checks[:3]))
    lines.append(f"_trace: `triage show {run_id}`_")
    return "\n".join(lines)


def post(alert: Alert, diag: Diagnosis, run_id: str, url: str | None = None,
         client: httpx.Client | None = None, path: Path = STATE_DB, attempts: int = 3,
         backoff: float = 1.0) -> str:
    """Post once per alert. Returns 'posted', 'duplicate', 'failed' or 'no_webhook'."""
    url = url or os.environ.get("TRIAGE_WEBHOOK_URL")
    if not url:
        return "no_webhook"
    if not claim(alert.alert_id, run_id, path):
        return "duplicate"
    text = format_message(alert, diag, run_id)
    client = client or httpx.Client(timeout=10)
    for i in range(attempts):
        try:
            resp = client.post(url, json={"text": text, "content": text})
            if resp.status_code < 500:
                resp.raise_for_status()
                _mark(alert.alert_id, "posted", path)
                return "posted"
        except httpx.HTTPStatusError:
            break  # 4xx: retrying will not help
        except httpx.TransportError:
            pass
        if i < attempts - 1:
            time.sleep(backoff * 2 ** i)
    _mark(alert.alert_id, "failed", path)
    return "failed"
