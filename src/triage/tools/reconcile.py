"""reconcile_counts: the loader invariant rows_read = loaded + rejected + deduped, as a tool."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from triage.tools.registry import Incident, Output, Tool, ToolError
from triage.tools.sql import execute


class ReconcileArgs(BaseModel):
    run_date: str | None = Field(default=None, description="YYYY-MM-DD")
    file_name: str | None = Field(default=None, description="e.g. orders_2026-10-21.csv")

    @model_validator(mode="after")
    def one_of(self):
        if not self.run_date and not self.file_name:
            raise ValueError("give run_date or file_name")
        return self


def reconcile_counts(inc: Incident, a: ReconcileArgs) -> Output:
    where, param = ("run_date = ?", a.run_date) if a.run_date else ("file_name = ?", a.file_name)
    _, rows, _ = execute(
        inc.db,
        "SELECT file_name, run_date, rows_read, rows_loaded, rows_rejected, rows_deduped "
        f"FROM ingest_manifest WHERE {where}", (param,))
    run_date = a.run_date or (rows[0][1] if rows else None)
    in_table = None
    if run_date:
        in_table = execute(inc.db, "SELECT COUNT(*) FROM orders WHERE run_date = ?",
                           (run_date,))[1][0][0]
    if not rows:
        if run_date is None:
            raise ToolError(f"no manifest row for file '{a.file_name}'")
        return Output(f"run_date={run_date} manifest=none orders_in_table={in_table}",
                      note="no manifest row: the file was never validated or loaded")
    lines = []
    for fname, rd, read, loaded, rejected, deduped in rows:
        gap = read - loaded - rejected - deduped
        lines.append(
            f"file={fname} run_date={rd} rows_read={read} rows_loaded={loaded} "
            f"rows_rejected={rejected} rows_deduped={deduped} gap={gap} "
            f"orders_in_table={in_table if rd == run_date else 'n/a'}")
    gaps = [ln for ln in lines if " gap=0 " not in ln]
    note = ("gap is nonzero: rows disappeared inside the loader" if gaps
            else "manifest reconciles (gap=0)")
    if in_table is not None and rows and in_table != rows[0][3]:
        note += "; orders_in_table differs from rows_loaded"
    return Output("\n".join(lines), note=note)


RECONCILE_COUNTS = Tool(
    "reconcile_counts",
    "Check the loader invariant for one run_date or file: rows_read should equal "
    "rows_loaded + rows_rejected + rows_deduped, and the orders table should hold rows_loaded "
    "rows. Use first when an alert mentions missing or low volume. A nonzero gap means rows "
    "disappeared inside the loader; gap=0 with low rows_read means less data arrived.",
    ReconcileArgs, reconcile_counts)
