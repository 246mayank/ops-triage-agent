"""triage CLI: build-incidents, run, show, eval, gate."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from triage.agent import DEFAULT_PROMPT

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="ops-triage-agent: diagnose data-pipeline alerts with grounded evidence")


@app.command("build-incidents")
def build_incidents(check: bool = typer.Option(False, help="Fail if anything would change")):
    """Generate every incident from its spec.yaml."""
    from sandbox.build import build_all

    ok, diff = build_all(check=check)
    if check and not ok:
        typer.echo("incidents are out of date with the generator:")
        for d in diff:
            typer.echo(f"  {d}")
        raise typer.Exit(1)
    typer.echo("incidents up to date" if check else "incidents built")


@app.command()
def run(incident: Path, post: bool = typer.Option(False, help="Post the diagnosis to chat"),
        model: str = typer.Option(None, help="Gemini model name"),
        prompt: str = typer.Option(DEFAULT_PROMPT, help="Prompt version"),
        cassette: Path = typer.Option(None, help="Record to / replay from this cassette")):
    """Investigate one incident and print the diagnosis."""
    from triage.agent import run_incident
    from triage.llm.gemini import DEFAULT_MODEL, GeminiClient
    from triage.llm.replay import RecordingClient

    llm = GeminiClient(model or DEFAULT_MODEL)
    if cassette:
        llm = RecordingClient(llm, cassette)
    res = run_incident(incident, llm, prompt_version=prompt)
    typer.echo(json.dumps(res.diagnosis.model_dump(mode="json"), indent=2))
    typer.echo(f"end: {res.end_reason}  cost: {res.trace.cost()}  trace: {res.trace_path}")
    if post:
        from triage import notify
        from triage.schema import Alert

        alert = Alert.model_validate_json((incident / "alert.json").read_text(encoding="utf-8"))
        typer.echo(f"post: {notify.post(alert, res.diagnosis, res.trace.run_id)}")


@app.command()
def show(run_id: str, runs_dir: Path = typer.Option(Path("runs"))):
    """Render a run trace as markdown."""
    from triage.trace import render_markdown

    path = Path(run_id)
    if not path.exists():
        matches = sorted(runs_dir.rglob(f"{run_id}*.jsonl"))
        if not matches:
            typer.echo(f"no trace found for {run_id}")
            raise typer.Exit(1)
        path = matches[-1]
    sys.stdout.reconfigure(encoding="utf-8")
    typer.echo(render_markdown(path))


@app.command("eval")
def eval_cmd(split: str = typer.Option("dev", help="dev, test or all"),
             runs: int = typer.Option(1),
             systems: str = typer.Option("agent,b0,b1"),
             model: str = typer.Option(None),
             prompt: str = typer.Option(DEFAULT_PROMPT),
             replay: bool = typer.Option(False, help="Cassettes only; never call the model"),
             incidents: str = typer.Option(None, help="Comma-separated incident names"),
             sleep: float = typer.Option(0.0, help="Seconds to wait after live calls")):
    """Run the eval and write reports to eval/results/."""
    from eval.run_eval import run_eval

    rep = run_eval(split, tuple(systems.split(",")), runs, model, prompt, replay,
                   sleep=sleep, incidents=incidents.split(",") if incidents else None)
    sys.stdout.reconfigure(encoding="utf-8")
    for s, v in rep["summary"].items():
        typer.echo(f"{s}: strict {v['strict_mean']}/{v['incidents']}  pass^k {v['pass_k']}  "
                   f"category {v['category_mean']}  ungrounded {v['ungrounded_final']}")
    for p in rep.get("paths", []):
        typer.echo(f"wrote {p}")


@app.command()
def gate(report: Path, baseline: Path = typer.Option(Path("eval/baseline.json"))):
    """Fail if a report regresses against the accepted baseline."""
    from eval.gate import check

    ok, lines = check(json.loads(report.read_text(encoding="utf-8")),
                      json.loads(baseline.read_text(encoding="utf-8")))
    for ln in lines:
        typer.echo(ln)
    raise typer.Exit(0 if ok else 1)


if __name__ == "__main__":
    app()
