---
description: Resume an interrupted /run from RUN_STATE.json. Refuses if the run was finalized (use a new project for follow-up work).
allowed-tools: Bash, Read, Write, Edit, Task
argument-hint: "[project_name]"
---

# /resume

You are resuming `/run` for project `$1`.

## What this command does

1. Read `<project>/RUN_STATE.json`. If missing → "No state to resume; start with `/run`."
2. If `state.finalized == true` → refuse. Tell the user: "This project is finalized; FINAL.md is at `<path>`. To extend work, run `genai new-project <newname> --fork=<project>`."
3. If `state.terminated == true` and not finalized → call the Curator to finalize.
4. Otherwise, re-construct the Orchestrator and call `/run`'s inner loop starting from `state.current_iteration`.

## Rules

- **Same Mission.** /resume never re-prompts for goal/floor/budget. Mission is immutable.
- **Same eval set.** If `EvalSet.content_hash() != Mission.eval_set_hash` (e.g. user edited eval_set.jsonl), refuse and direct user to `genai new-project --fork`.
- **One short status line on resume:** `Resuming iter N, budget $X.XX of $Y.YY spent, breakthrough=<active|off>`.
