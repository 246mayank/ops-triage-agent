"""Runs the agent and the baselines over a split, k runs each, and writes a JSON + markdown
report to eval/results/. Every model response is cached in a cassette keyed by
(incident, system, run), so a re-score or report rebuild never calls the model again."""

from __future__ import annotations

import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

from eval.baselines import run_baseline
from eval.score import Expected, score
from triage.agent import DEFAULT_PROMPT, Budget, run_incident
from triage.llm.replay import RecordingClient, ReplayClient
from triage.schema import Alert
from triage.trace import git_commit
from triage.validate import downgrade, validate

ROOT = Path(__file__).resolve().parent.parent
INCIDENTS = ROOT / "incidents"
RESULTS = ROOT / "eval" / "results"
CASSETTES = ROOT / "eval" / "cassettes"
ABSTAIN = {"no_fault", "insufficient_evidence"}
INJECTION_INCIDENT = "15_injection_in_logs"


class LazyGemini:
    """Creates the live client only when a request misses the cassette."""

    def __init__(self, model: str):
        self.model = model
        self._inner = None

    def chat(self, messages, tools, force_tool=None):
        if self._inner is None:
            from triage.llm.gemini import GeminiClient
            self._inner = GeminiClient(self.model)
        return self._inner.chat(messages, tools, force_tool)


def incidents_for(split: str) -> list[Path]:
    out = []
    for d in sorted(INCIDENTS.iterdir()):
        if (d / "expected.yaml").exists():
            exp = Expected.load(d)
            if split == "all" or exp.split == split:
                out.append(d)
    return out


def _client(model: str, system: str, incident: str, k: int, replay: bool, base,
            cassettes: Path):
    path = cassettes / model / system / incident / f"run{k}.json"
    salt = f"{system}:{incident}:{k}"
    if replay:
        return ReplayClient(path, model, salt)
    return RecordingClient(base, path, salt)


def run_one(system: str, inc: Path, k: int, model: str, prompt_version: str, replay: bool,
            base, cassettes: Path, trace_dir: Path, budget: Budget) -> dict:
    exp = Expected.load(inc)
    llm = _client(model, system, inc.name, k, replay, base, cassettes)
    rec = {"incident": inc.name, "system": system, "run": k, "expected_status": exp.status,
           "expected_category": exp.category}
    if system == "agent":
        res = run_incident(inc, llm, budget, prompt_version, run_id=f"{inc.name}-{system}-r{k}",
                           trace_dir=trace_dir)
        diag, ungrounded = res.diagnosis, res.trace.ungrounded_quotes_final
        cost = res.trace.cost()
        rec.update(end_reason=res.end_reason, quotes_caught=res.trace.quotes_caught,
                   repairs=res.trace.repairs_used, trace=res.trace_path.name)
    else:
        alert = Alert.model_validate_json((inc / "alert.json").read_text(encoding="utf-8"))
        turn, results, _ = run_baseline(system, alert, inc, llm, prompt_version)
        call = next((c for c in turn.tool_calls if c.name == "submit_diagnosis"), None)
        diag, errs, ungrounded = validate(call.args, results) if call else (None, ["no call"], 0)
        caught = ungrounded
        if errs:  # same rule as the agent's final submission: invalid -> downgraded
            diag, ungrounded = downgrade(diag, errs), 0
        cost ={"turns": 1, "tool_calls": 0, "input_tokens": turn.usage.input_tokens,
                "output_tokens": turn.usage.output_tokens,
                "seconds": round(turn.usage.latency_ms / 1000, 2)}
        rec.update(end_reason="validation_failed" if errs else "submitted", quotes_caught=caught,
                   repairs=0, validation_errors=errs)
    # cost["seconds"] is model latency + tool time from the recorded turns, so replays report
    # the same timing as the live run (rate-limit waits are excluded).
    s = score(diag, exp, ungrounded, cost)
    rec.update(score=s.to_dict(), diagnosis=diag.model_dump(mode="json") if diag else None,
               live_calls=getattr(llm, "live_calls", 0))
    return rec


def summarise(records: list[dict], systems: list[str], runs: int) -> dict:
    out = {}
    incidents = sorted({r["incident"] for r in records})
    for sysname in systems:
        rs = [r for r in records if r["system"] == sysname]
        by_run = {k: [r for r in rs if r["run"] == k] for k in range(1, runs + 1)}
        strict_per_run = [sum(r["score"]["strict"] for r in v) for v in by_run.values()]
        cat_per_run = [sum(r["score"]["category_ok"] for r in v) for v in by_run.values()]
        pass_k = sum(all(r["score"]["strict"] for r in rs if r["incident"] == i)
                     for i in incidents)
        abst = [r for r in rs if r["expected_status"] in ABSTAIN]
        false_abst = [r for r in rs if r["expected_status"] == "diagnosed"
                      and r["diagnosis"] and r["diagnosis"]["status"] != "diagnosed"]
        inj = [r for r in rs if r["incident"] == INJECTION_INCIDENT]
        secs = sorted(r["score"]["cost"].get("seconds", 0) for r in rs)
        out[sysname] = {
            "incidents": len(incidents), "runs": runs, "attempts": len(rs),
            "strict_mean": round(statistics.mean(strict_per_run), 2) if rs else 0,
            "strict_per_run": strict_per_run,
            "strict_attempts": sum(r["score"]["strict"] for r in rs),
            "category_mean": round(statistics.mean(cat_per_run), 2) if rs else 0,
            "pass_k": pass_k,
            "quotes_caught": sum(r.get("quotes_caught", 0) for r in rs),
            "ungrounded_final": sum(r["score"]["ungrounded"] for r in rs),
            "validation_failed": sum(r.get("end_reason") == "validation_failed" for r in rs),
            "abstention_correct": f"{sum(r['score']['category_ok'] for r in abst)}/{len(abst)}",
            "false_abstentions": len(false_abst),
            "injection": f"{sum(r['score']['strict'] for r in inj)}/{len(inj)}" if inj else "n/a",
            "median_tool_calls": statistics.median(r["score"]["cost"].get("tool_calls", 0)
                                                   for r in rs) if rs else 0,
            "median_turns": statistics.median(r["score"]["cost"].get("turns", 0)
                                              for r in rs) if rs else 0,
            "total_tokens": sum(r["score"]["cost"].get("input_tokens", 0)
                                + r["score"]["cost"].get("output_tokens", 0) for r in rs),
            "p50_seconds": secs[len(secs) // 2] if secs else 0,
            "p95_seconds": secs[min(len(secs) - 1, int(round(0.95 * (len(secs) - 1))))]
            if secs else 0,
        }
    return out


def render_markdown(report: dict) -> str:
    meta, summ = report["meta"], report["summary"]
    n = next(iter(summ.values()))["incidents"] if summ else 0
    lines = [f"# Eval report: {meta['eval_id']}", "",
             f"- split: **{meta['split']}** ({n} incidents), runs per incident: "
             f"**{meta['runs']}**",
             f"- model: `{meta['model']}`, prompt: `{meta['prompt_version']}`, commit: "
             f"`{meta['commit']}`, created {meta['created']}",
             f"- replayed from cassettes: {meta['replay']}", "",
             "## Summary", "",
             "| system | strict correct (mean of runs) | category acc. (mean) | pass^k | "
             "quotes caught | ungrounded accepted | abstention | false abstentions | "
             "injection | median tool calls | median turns | total tokens | p50 s | p95 s |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for s, v in summ.items():
        lines.append(
            f"| {s} | {v['strict_mean']}/{n} (attempts {v['strict_attempts']}/{v['attempts']}) "
            f"| {v['category_mean']}/{n} | {v['pass_k']}/{n} | {v['quotes_caught']} | "
            f"{v['ungrounded_final']} | {v['abstention_correct']} | {v['false_abstentions']} | "
            f"{v['injection']} | {v['median_tool_calls']} | {v['median_turns']} | "
            f"{v['total_tokens']} | {v['p50_seconds']} | {v['p95_seconds']} |")
    systems = list(summ)
    lines += ["", "## Per incident (✓ strict correct, ~ category right but evidence short, "
              "✗ wrong; one mark per run)", "",
              "| incident | expected | " + " | ".join(systems) + " |",
              "|---|---|" + "---|" * len(systems)]
    incidents = sorted({r["incident"] for r in report["records"]})
    for inc in incidents:
        rs = [r for r in report["records"] if r["incident"] == inc]
        exp = rs[0]["expected_category"] or rs[0]["expected_status"]
        cells = []
        for s in systems:
            marks = ""
            for r in sorted((r for r in rs if r["system"] == s), key=lambda r: r["run"]):
                sc = r["score"]
                marks += "✓" if sc["strict"] else ("~" if sc["category_ok"] else "✗")
            got = [r["diagnosis"]["category"] or r["diagnosis"]["status"]
                   for r in rs if r["system"] == s and r["diagnosis"]]
            wrong = sorted({g for g in got if g != exp})
            cells.append(marks + (f" ({', '.join(wrong)})" if wrong else ""))
        lines.append(f"| {inc} | {exp} | " + " | ".join(cells) + " |")
    lines += ["", "Strict correct = status and category right, at least `min_evidence_hits` "
              "required evidence strings cited, and 0 ungrounded quotes. pass^k = incidents "
              "strict-correct on every run. Scoring is deterministic (string match), no LLM "
              "judge.", ""]
    return "\n".join(lines)


def run_eval(split: str = "dev", systems: tuple[str, ...] = ("agent", "b0", "b1"),
             runs: int = 1, model: str | None = None, prompt_version: str = DEFAULT_PROMPT,
             replay: bool = False, out_dir: Path = RESULTS, cassettes: Path = CASSETTES,
             sleep: float = 0.0, budget: Budget | None = None,
             incidents: list[str] | None = None, write: bool = True) -> dict:
    from triage.llm.gemini import DEFAULT_MODEL

    model = model or DEFAULT_MODEL
    commit = git_commit()
    created = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    eval_id = f"{created[:10]}_{split}_{model}_{prompt_version}_{commit}"
    trace_dir = ROOT / "runs" / "eval" / eval_id
    base = LazyGemini(model)
    dirs = incidents_for(split)
    if incidents:
        dirs = [d for d in dirs if d.name in incidents]
    records = []
    for inc in dirs:
        for k in range(1, runs + 1):
            for sysname in systems:
                rec = run_one(sysname, inc, k, model, prompt_version, replay, base, cassettes,
                              trace_dir, budget or Budget())
                records.append(rec)
                sc = rec["score"]
                mark = "PASS" if sc["strict"] else ("cat" if sc["category_ok"] else "FAIL")
                got = rec["diagnosis"] and (rec["diagnosis"]["category"]
                                            or rec["diagnosis"]["status"])
                print(f"{inc.name:28s} run{k} {sysname:5s} {mark:4s} got={got} "
                      f"hits={sc['hits']} {rec.get('end_reason')}", flush=True)
                if sleep and rec.get("live_calls"):
                    time.sleep(sleep)
    report = {"meta": {"eval_id": eval_id, "split": split, "runs": runs, "model": model,
                       "prompt_version": prompt_version, "commit": commit, "created": created,
                       "replay": replay, "systems": list(systems)},
              "summary": summarise(records, list(systems), runs), "records": records}
    if write:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{eval_id}.json").write_text(json.dumps(report, indent=1),
                                                 encoding="utf-8", newline="\n")
        (out_dir / f"{eval_id}.md").write_text(render_markdown(report), encoding="utf-8",
                                               newline="\n")
        report["paths"] = [str(out_dir / f"{eval_id}.json"), str(out_dir / f"{eval_id}.md")]
    return report
