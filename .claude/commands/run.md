---
description: Autonomous optimization loop. Iterates Strategist → Sentinel → Auditor → Operator → (every 5) Inspector + Provoker until termination. Then runs Curator → FINAL.md.
allowed-tools: Bash, Read, Write, Edit, Task
argument-hint: "[project_name] [--max-iterations N]"
---

# /run

You are entering `/run` for project `$1`. Optional cap: `$2`.

## Preconditions

- `MISSION.json` exists. Else direct user to `/plan`.
- `experiment_log.jsonl` has at least a baseline trial. Else direct user to `/init`.
- `RUN_STATE.json` either doesn't exist OR has `terminated == false`. If terminated, refuse — direct user to `/resume` (which itself refuses if finalized) or `genai new-project --fork`.

## The loop

```
while True:
    brief = orchestrator.next()              # IterationBrief
    if breakthrough.active and entries_consumed == 0:
        invoke(scout)                         # SOTA paper grounding
    propose_attempts = 0
    while True:
        invoke(strategist)
        invoke(sentinel)
        if sentinel.verdict == "duplicate":
            propose_attempts += 1
            if propose_attempts >= 3: invoke(mediator); continue
            continue
        invoke(auditor)
        if auditor.verdict == "ACCEPT" or "WARN":
            break
        if auditor.verdict == "CATASTROPHIC":
            terminate("catastrophic_auditor")
        propose_attempts += 1
        if propose_attempts >= 3: invoke(mediator)
    invoke(operator)
    if iteration % 5 == 0 and iteration > 0:
        invoke(inspector)
        invoke(provoker)
    reason = orchestrator.record_trial(trial)
    if reason: break
# Terminated:
invoke(curator)
```

## Rules

- **Hard cap at `mission.max_iterations`.** Override only with `--max-iterations`.
- **Print one short status line per iteration.** Format:
  > `[iter 7] arm=prompt_rewrite → judge_score 0.62 ± 0.04 (Δ +0.03) [$0.21/$50 budget]`
- **Never edit the experiment_log.jsonl directly.** All writes go through `lib.run.execute_trial`.
- **On Ctrl-C** — set `RunState.terminated_reason = "user_interrupt"` and save state atomically; do not run Curator.

## Output

After termination, print the FINAL.md path and the confidence tier.
