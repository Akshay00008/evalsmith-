---
description: Compact one-screen project status — current iter, best metric, budget, breakthrough state, top failure clusters.
allowed-tools: Bash, Read
argument-hint: "[project_name]"
---

# /status

You are reporting status for project `$1`.

## What this command does

Read-only summary. No subagents invoked.

1. Read `PROJECT.json` (status), `MISSION.json` (criteria), `RUN_STATE.json` (current iter), `budget.jsonl` (spend), and the experiment log tail (last 3 trials).
2. Read `sketch.queries.failure_clusters(top_n=3)` and `cost_breakdown(last_n=3)`.
3. Render a single compact block:

```
PROJECT  support_bot_v2          STATUS   running
MISSION  judge_score >= 0.85     FLOOR    0.70
ITER     12 / 30                 BUDGET   $7.41 / $50.00
BEST     judge_score = 0.74 (trial a3f9c2d18b06e145) — ↑ +0.06 vs baseline
BREAKTHROUGH  inactive (0/3 used)

Last 3 trials:
  iter 10  arm=prompt_rewrite       judge=0.71  cost=$0.18
  iter 11  arm=few_shot_selection   judge=0.73  cost=$0.21
  iter 12  arm=prompt_rewrite       judge=0.74  cost=$0.19

Top failure clusters:
  1. over_refuses_safe_queries          (n=8)
  2. cites_wrong_doc_id                 (n=5)
  3. omits_evidence_span                (n=3)
```

## Rules

- **Single-screen.** Never paginate, never expand to >30 lines.
- **Never trigger writes.** /status must be safely runnable while /run is active.
