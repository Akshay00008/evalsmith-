---
description: Profile the eval set + corpus and emit INIT_PROFILE.json. Pure inspection — no questions, no LLM judgment calls. Builds the E1 layer of the sketch and runs the baseline trial.
allowed-tools: Bash, Read, Write, Edit
argument-hint: "[project_name]"
---

# /init

You are entering the `/init` slash command for the **AgenticGenAIDevTool** framework. The project name to initialize is: `$1` (default: current directory's basename).

## What this command does

1. Locate the project workspace under `projects/$1/`. If it doesn't exist, **stop and tell the user** to run `genai new-project $1` first — `/init` doesn't create projects, only profiles them.
2. Discover the eval set at `projects/$1/data/eval_set.jsonl` (or `.json`). If missing, halt with a clear error.
3. Load the eval set via `lib.schemas.EvalSet`. Compute `content_hash()` — this is what `/plan` pins.
4. Build the E1 layer via `lib.sketch.build_sketch(project_dir, mission_id="pending", eval_set=...)`.
5. Run a *baseline trial* using the universal seed hypothesis #1 ("naive baseline"). This populates one row in `experiment_log.jsonl` so `/plan` has a reference point.
6. Write `<project>/INIT_PROFILE.json` with:
   - eval set size + tags + length stats (from E1)
   - baseline trial id + primary metric value
   - estimated cost per full eval pass
7. Update `<project>/PROJECT.json` `status: "initialized"`.

## Rules

- **No user questions.** `/init` is read-only inspection. Disambiguation belongs in `/plan`.
- **Cap baseline cost.** Estimate first; if running the baseline would exceed $5 USD, sample the eval set down to ~100 cases and flag this in the profile.
- **Be deterministic.** Same eval set → same INIT_PROFILE.json.

## Output

A single concise message to the user:

> Initialized `<project>`. Eval set: 247 cases, 6 tags, avg input len 312 chars. Baseline `judge_score`: 0.41 ± 0.06. Estimated full-pass cost ~$0.18. Ready for `/plan`.
