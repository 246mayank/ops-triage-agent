"""get_job_history: recent runs of a job from the jobs table."""

from __future__ import annotations

from pydantic import BaseModel, Field

from triage.tools.registry import Incident, Output, Tool
from triage.tools.sql import execute, format_table

MAX_RUNS = 20


class JobHistoryArgs(BaseModel):
    job_name: str = Field(description="Job name, e.g. order_sync")
    limit: int | None = Field(default=None, description=f"Max runs, at most {MAX_RUNS}")


def get_job_history(inc, a: JobHistoryArgs) -> Output:
    limit = min(a.limit or MAX_RUNS, MAX_RUNS)
    cols, rows, _ = execute(
        inc.db,
        "SELECT job_id, run_date, status, attempts, started_at, finished_at, error "
        "FROM jobs WHERE job_name = ? ORDER BY run_date DESC LIMIT ?",
        (a.job_name, limit))
    if not rows:
        names = [r[0] for r in execute(inc.db, "SELECT DISTINCT job_name FROM jobs")[1]]
        return Output("", note=f"no runs for '{a.job_name}'; known jobs: {', '.join(names)}")
    return Output(format_table(cols, rows), note="newest run_date first")


GET_JOB_HISTORY = Tool(
    "get_job_history",
    "Recent runs of a job (newest first) with status, attempts, start/finish times and error. "
    "Statuses: PENDING, DOWNLOADED, VALIDATED, LOADED, FAILED. Usually one of the first calls. "
    "A missing run_date means the job never started for that date.",
    JobHistoryArgs, get_job_history)

__all__ = ["GET_JOB_HISTORY", "Incident"]
