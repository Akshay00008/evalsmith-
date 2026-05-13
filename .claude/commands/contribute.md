---
description: Stage the anonymized knowledge bundle from a finalized project for merge into the shared knowledge/ library. Refuses if Curator hasn't run.
allowed-tools: Bash, Read, Write, Edit
argument-hint: "[project_name]"
---

# /contribute

You are staging knowledge contribution for project `$1`.

## What this command does

1. Read `<project>/RUN_STATE.json`. If `finalized == false` → "Run /run to finalize first."
2. Read `<project>/results/knowledge_bundle.json` (produced by the Curator).
3. Run `tools/post_merge_extractor.py --dry-run --project <project>` to verify anonymization. If it reports any raw eval text or PII, refuse and tell the user which fields need scrubbing.
4. If clean, stage the bundle into the framework root's `knowledge/` files:
   - Append the winning prompt pattern → `knowledge/prompt_pattern_library.jsonl`
   - Append the winning RAG recipe (if applicable) → `knowledge/rag_recipes.jsonl`
   - Append persistent failure clusters → `knowledge/failure_modes.jsonl`
   - Append model routing observations → `knowledge/model_route_priors.jsonl`
   - Append judge template (if any) → `knowledge/eval_judge_templates.jsonl`
5. Write `<project>/CONTRIBUTION.md` containing the diff summary + the git merge-PR command for the user to run.

## Rules

- **Anonymize, always.** Field names and free text both. Raw eval inputs/outputs never reach `knowledge/`.
- **Append-only.** Never edit or remove existing knowledge entries; superseding entries get a new id with `supersedes: <old_id>`.
- **Idempotent.** Running /contribute twice on the same project produces zero diff on the second run.
