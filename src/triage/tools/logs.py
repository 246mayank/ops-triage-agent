"""search_logs: regex search over one service's JSON-lines log, newest first."""

from __future__ import annotations

import json

import regex
from pydantic import BaseModel, Field

from triage.tools.registry import Incident, Output, Tool, ToolError, trim

SERVICES = ["scheduler", "downloader", "validator", "loader"]
MAX_LINES = 40
REGEX_TIMEOUT = 1.0
LEVELS = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}


def safe_path(inc: Incident, sub: str, name: str) -> object:
    """Resolve name under inc.root/sub and refuse anything that escapes it."""
    base = (inc.root / sub).resolve()
    target = (base / name).resolve()
    if not target.is_relative_to(base):
        raise ToolError(f"path '{name}' is outside the allowed folder '{sub}/'")
    return target


class SearchLogsArgs(BaseModel):
    service: str = Field(description="One of: " + ", ".join(SERVICES) + ", or 'all'")
    pattern: str = Field(default="", max_length=200,
                         description="Case-insensitive regex matched against the message; "
                                     "empty matches every line")
    level: str | None = Field(default=None,
                              description="Minimum level: INFO, WARN or ERROR")
    since: str | None = Field(default=None, description="ISO time, inclusive (UTC)")
    until: str | None = Field(default=None, description="ISO time, inclusive (UTC)")
    job_id: str | None = Field(default=None, description="e.g. os-20261021")
    limit: int | None = Field(default=None, description=f"Max lines, at most {MAX_LINES}")


def format_line(d: dict) -> str:
    return f"{d['ts']} {d['level']} {d['service']} [{d.get('job_id') or '-'}] {d['msg']}"


def search_logs(inc: Incident, a: SearchLogsArgs) -> Output:
    services = SERVICES if a.service == "all" else [a.service]
    if a.service != "all" and a.service not in SERVICES:
        raise ToolError(f"unknown service '{a.service}'; use one of {', '.join(SERVICES)} or all")
    min_level = None
    if a.level:
        lv = a.level.upper().replace("WARNING", "WARN")
        if lv not in LEVELS:
            raise ToolError("level must be INFO, WARN or ERROR")
        min_level = LEVELS[lv]
    try:
        rx = regex.compile(a.pattern, regex.IGNORECASE)
    except regex.error as e:
        raise ToolError(f"invalid regex: {e}") from None
    limit = min(a.limit or MAX_LINES, MAX_LINES)

    matches: list[dict] = []
    for svc in services:
        path = safe_path(inc, "logs", f"{svc}.jsonl")
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            d = json.loads(raw)
            if min_level is not None and LEVELS.get(d["level"], 0) < min_level:
                continue
            if a.job_id and d.get("job_id") != a.job_id:
                continue
            if a.since and d["ts"] < a.since:
                continue
            if a.until and d["ts"] > a.until:
                continue
            try:
                if a.pattern and not rx.search(d["msg"], timeout=REGEX_TIMEOUT):
                    continue
            except TimeoutError:
                raise ToolError("regex took too long; simplify the pattern") from None
            matches.append(d)
    matches.sort(key=lambda d: d["ts"], reverse=True)
    shown = matches[:limit]
    if not shown:
        return Output("", note="0 matching lines")
    out = trim("\n".join(format_line(d) for d in shown), hint="narrow the pattern or time range")
    extra = len(matches) - len(shown)
    notes = [f"{len(matches)} matching lines, newest first"]
    if extra > 0:
        out.truncated = True
        notes.append(f"truncated: {extra} more lines not shown; narrow with level, since/until, "
                     "job_id or a tighter pattern")
    if out.note:
        notes.append(out.note)
    out.note = "; ".join(notes)
    return out


SEARCH_LOGS = Tool(
    "search_logs",
    "Search a service's logs (scheduler, downloader, validator, loader, or all). Returns "
    "matching lines newest first, at most 40. Filter by level, job_id (os-YYYYMMDD) and "
    "time to cut noise: repeated deprecation warnings and slow-query warnings are normal "
    "background. INFO lines often hold the detail (hosts, counts, settings), so do not "
    "only search WARN/ERROR.",
    SearchLogsArgs, search_logs)
