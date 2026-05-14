---
description: Adaptive Q&A → lock MISSION.json + seed HYPOTHESES.jsonl. Invokes the Architect subagent. Refuses to plan a project whose eval set is too small to be reliable.
allowed-tools: Bash, Read, Write, Edit, Task
argument-hint: "[project_name] [--recipe <recipe_name>]"
---

# /plan

You are entering `/plan` for project `$1`. Optional recipe: `$2` (e.g. `--recipe support_bot`).

## What this command does

1. Confirm `<project>/INIT_PROFILE.json` exists. If not, instruct the user to `/init` first.
2. Read INIT_PROFILE to ground your questions in real data (don't ask "how big is your eval set?" — you already know).
3. Invoke the **Architect** subagent (Task tool, subagent_type=architect). Pass the user's goal verbatim plus the init profile path.
4. The Architect conducts adaptive Q&A. *Minimum* questions to ask (only those not already answered by recipe / init profile):
   - **Task modality** — one of `rag_qa | nlq_to_query | research_agent | insight_agent | search_engine | chatbot`.
   - **Primary metric** — picked from the capability's `primary_metrics` list. If unclear, ask.
   - **Operational floor** — "below this metric value, the project has failed". This must come from the user, not be inferred.
   - **Total budget USD** (default 50).
   - **Domain** — picked from `lib/domains/*` or `general`.
   - **Eval strategy** — `judge_llm | exact_match | tool_call_match | ...`. If judge_llm, ask for a judge model (default `claude-sonnet-4-5`) and whether gold calibration labels exist.
5. The Architect writes `MISSION.json` (lib.schemas.Mission) and `memory/HYPOTHESES.jsonl` (filter `seeds/universal_seeds.jsonl` by capability + domain).
6. Print a 4-line summary of the locked Mission to the user.

## Rules

- **Refuse if eval set < 20 cases.** Tell the user: "Eval set too small to reliably drive optimization. Grow it to ≥20 before /plan."
- **Refuse if INIT_PROFILE doesn't exist.** Don't try to compute it inline — /init is the source of truth.
- **Recipes load defaults; never lock without confirmation.** Show the user the proposed Mission and ask "lock? (y/n)" before writing.
