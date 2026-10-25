# Prompt and agent changelog

Dev-split scores only. The test split is run once, after the freeze.

| version | date | change | dev strict (agent) | report |
|---|---|---|---|---|
| v1 | 2026-10-24 | Plan's prompt. Model gemini-3.5-flash-lite. B0 0/10, B1 7/10 (B1 count used the old lenient baseline check, see below). | 7/10 | [md](results/2026-10-24_dev_gemini-3.5-flash-lite_v1_6916ce0.md) |
| v2 | 2026-10-24 | Stop rule: submit once two independent results agree; aim for 3-6 calls. Agent only, old harness. Cheaper (p50 30 s vs 54 s, -21% tokens); misses 03/13 were spliced table quotes, 08 a wrong cause. | 6/10 | [md](results/2026-10-24_dev_gemini-3.5-flash-lite_v2_6916ce0.md) |
| v3 | 2026-10-24 | Evidence should show the failure, when it started, and why the pipeline did not recover. First run on the fixed harness; B0 0/10, B1 5/10 on the same prompt. Category 9/10; misses are mostly short evidence (03, 04, 05), 13 wrong. | 6/10 | [md](results/2026-10-24_dev_gemini-3.5-flash-lite_v3_6916ce0.md) |
| v4 | 2026-10-25 | Always check context before submitting: config_history and past alerts for the rule. 2 runs: stable 6/10 each, pass^2 6/10; 05 now passes, 13 right status but quotes neither required string, 08 wrong both runs. | 6/10 (12/20) | [md](results/2026-10-24_dev_gemini-3.5-flash-lite_v4_6916ce0.md) |
| v5 | 2026-10-25 | Narrow a truncated log search (level=WARN); current policy via read_config; no_fault quotes counts that reconcile. 2 runs: 7/10 mean (14/20), category 9.5/10, p50 14 s; 13 passes both runs. **Frozen.** Same prompt, 2 runs: B0 0/10, B1 4.5/10 (4 made-up quotes caught). | 7/10 (14/20) | [md](results/2026-10-25_dev_gemini-3.5-flash-lite_v5_6916ce0.md) |

Harness changes after v1 (apply to all systems equally):
- A category sent with `no_fault`/`insufficient_evidence` is dropped instead of rejected
  (agent 13 was downgraded only for this).
- Baseline answers with validation errors are now downgraded, like the agent's final
  answer. Before, any schema-valid baseline answer was scored (B1 13 passed with a
  category the agent was rejected for).
- The evidence `quote` field now states the 200-character limit (B1 08 failed on it).

Harness changes after v2:
- An invalid category (e.g. `no_fault` echoed into it) is also dropped when the status is
  `no_fault`/`insufficient_evidence`.
- The quote field and the repair message say a table quote is one row without the header
  (agent 03 and 13 joined a header to a non-adjacent row; the validator rightly refused).
