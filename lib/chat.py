# lib/chat.py
# Interactive chat REPL — lets you converse with a project's winning
# Variant after /run terminates. Two modes:
#
#   chatbot mission     → uses registry.chat_call (multi-turn) with the
#                         winning prompt + model + decoding params.
#   rag_qa mission      → wraps the user's message into the RAG pipeline:
#                         retrieve, build prompt, generate.
#   nlq mission         → generates SQL and (if DB configured) executes
#                         + shows the result table.
#   other capabilities  → falls back to single-turn model_call.
#
# Why this exists: pre-/run, you have no winning variant. Post-/run, the
# winner is the variant referenced in FINAL.md's `evidence_trial_ids[0]`.
# This REPL loads it and gives you a feel for what shipping the variant
# would actually be like — useful sanity check before deployment.

from __future__ import annotations
from pathlib import Path
from typing import Optional
import json
import sys

from .schemas import Mission, Variant, EvalSet, EvalCase
from .schemas.variant import PromptBundle, RetrievalConfig, GenerationConfig
from .schemas.trial import TrialResult
from . import registry as model_registry


# ---------------------------------------------------------------------------
# Loading the winning variant
# ---------------------------------------------------------------------------

def load_winning_variant(project_dir: Path, *, trial_id: Optional[str] = None) -> tuple[Mission, Variant]:
    """Resolve the variant we'll chat against.

    Resolution order:
      1. If `trial_id` is supplied, locate that trial in experiment_log.jsonl.
      2. Else, parse FINAL.md for the first evidence trial id.
      3. Else, pick the best-by-primary-metric trial in the log.

    Returns (Mission, Variant). Note that the log doesn't store the full
    Variant — only the variant_id. We reconstruct from prompt/retrieval/
    generation defaults plus any iteration-inbox-saved diffs. For v0 we
    just rebuild a Variant from the mission's domain seed + any saved
    `winning_variant.json` artifact written by the Curator.
    """
    mp = project_dir / "MISSION.json"
    if not mp.exists():
        raise FileNotFoundError(f"No MISSION.json at {mp}. Run /init and /plan first.")
    mission = Mission.model_validate_json(mp.read_text(encoding="utf-8"))

    # Prefer the Curator's pinned variant if present — most reliable.
    winning_path = project_dir / "results" / "winning_variant.json"
    if winning_path.exists():
        variant = Variant.model_validate_json(winning_path.read_text(encoding="utf-8"))
        return mission, variant

    # Fall back: pick the best trial in the log + reconstruct a Variant
    # from the domain's seed system prompt. This isn't lossless (we don't
    # know exactly what prompt/model the trial used) but gives a
    # functional REPL for smoke-testing.
    log_path = project_dir / "experiment_log.jsonl"
    if not log_path.exists():
        # No trials yet — fall back to the domain seed.
        return mission, _seed_variant_from_mission(mission)

    trials: list[TrialResult] = []
    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                trials.append(TrialResult.model_validate_json(line))
    if not trials:
        return mission, _seed_variant_from_mission(mission)

    primary_op = mission.primary_criterion().operator
    higher_better = primary_op in (">=", ">", "==")
    best = sorted(
        (t for t in trials if t.metrics),
        key=lambda t: t.metrics[0].value,
        reverse=higher_better,
    )[0] if any(t.metrics for t in trials) else trials[-1]

    # We don't have the full Variant — return the seed with a note.
    variant = _seed_variant_from_mission(mission)
    print(f"[chat] Using seed variant — winning trial was {best.trial_id} but the "
          f"framework didn't pin its full config. For a true reproduction, ask "
          f"the Curator to write results/winning_variant.json.", file=sys.stderr)
    return mission, variant


def _seed_variant_from_mission(mission: Mission) -> Variant:
    """Construct a Variant from the mission's domain seed. Used as the
    fallback when no winning variant has been pinned."""
    from .domains import get_domain
    domain = get_domain(mission.domain)
    prompt = PromptBundle(system=domain.seed_system_prompt or "You are a helpful assistant.")
    retrieval = RetrievalConfig(
        enabled=mission.composition.task_modality in ("rag_qa", "search_engine"),
        retriever_kind="bm25" if mission.composition.task_modality in ("rag_qa", "search_engine") else "none",
    )
    generation = GenerationConfig(model=domain.seed_model)
    return Variant(
        variant_id=Variant.compute_id(prompt, retrieval, generation),
        technique_family="prompt_rewrite",
        prompt=prompt, retrieval=retrieval, generation=generation,
    )


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

def run_repl(project_dir: Path, *, trial_id: Optional[str] = None, save_transcript: bool = True) -> None:
    """Start an interactive chat loop. Reads stdin one line per turn;
    Ctrl-D or `/exit` to quit. Transcripts saved to results/chat_log_*.jsonl
    if save_transcript is True (default)."""
    mission, variant = load_winning_variant(project_dir, trial_id=trial_id)
    modality = mission.composition.task_modality

    _print_banner(mission, variant)

    # Conversation buffer used only by the chatbot modality. For the
    # other modalities each turn is independent (no carried context).
    turns: list[dict] = []
    transcript: list[dict] = []

    while True:
        try:
            user_msg = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_msg:
            continue
        if user_msg in ("/exit", "/quit", ":q"):
            break
        if user_msg == "/help":
            _print_help()
            continue
        if user_msg == "/variant":
            print(json.dumps(variant.model_dump(), indent=2, default=str))
            continue
        if user_msg == "/reset" and modality == "chatbot":
            turns.clear()
            print("[chat] conversation reset")
            continue

        # Dispatch by modality.
        if modality == "chatbot":
            response = _turn_chatbot(variant, turns, user_msg)
        elif modality == "rag_qa":
            response = _turn_rag(variant, project_dir, user_msg)
        elif modality == "nlq_to_query":
            response = _turn_nlq(variant, project_dir, user_msg)
        else:
            response = _turn_single(variant, user_msg)

        print(f"\nbot> {response}")

        transcript.append({"turn": len(transcript), "user": user_msg, "assistant": response})

    # Persist transcript on exit (unless --no-transcript was passed).
    if save_transcript and transcript:
        import time
        out = project_dir / "results" / f"chat_log_{int(time.time())}.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as f:
            for row in transcript:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"\n[chat] transcript saved to {out}")


# ---------------------------------------------------------------------------
# Per-modality turn handlers
# ---------------------------------------------------------------------------

def _turn_chatbot(variant: Variant, turns: list[dict], user_msg: str) -> str:
    """Multi-turn chat — appends to the turn buffer and calls chat_call."""
    turns.append({"role": "user", "content": user_msg})
    call = model_registry.chat_call(
        system=variant.prompt.system,
        turns=turns,
        generation=variant.generation,
        few_shots=variant.prompt.few_shots,
    )
    turns.append({"role": "assistant", "content": call.text})
    return f"{call.text}\n   ({call.input_tokens}+{call.output_tokens} tok · ${call.cost_usd:.4f} · {int(call.latency_ms)}ms)"


def _turn_rag(variant: Variant, project_dir: Path, user_msg: str) -> str:
    """Single-turn RAG — retrieve, then generate. No multi-turn memory."""
    retrieved = model_registry.retrieve(
        query=user_msg, config=variant.retrieval, corpus_dir=project_dir,
    )
    ctx_block = "\n\n".join(f"[{d['doc_id']}] {d['text']}" for d in retrieved)
    rendered = variant.prompt.user_template.format(input=user_msg, context=ctx_block) \
        if "{context}" in variant.prompt.user_template else \
        f"Context:\n{ctx_block}\n\nQuestion: {user_msg}"
    call = model_registry.model_call(
        system=variant.prompt.system,
        user=rendered,
        generation=variant.generation,
        few_shots=variant.prompt.few_shots,
    )
    cite_str = ", ".join(d["doc_id"] for d in retrieved[:3])
    return (f"{call.text}\n   (retrieved: {cite_str} · "
            f"{call.input_tokens}+{call.output_tokens} tok · ${call.cost_usd:.4f})")


def _turn_nlq(variant: Variant, project_dir: Path, user_msg: str) -> str:
    """NLQ — generate SQL, then (if DB configured) execute it and render
    the result as a small ASCII table."""
    from .capabilities.nlq import _extract_sql
    rendered = variant.prompt.user_template.format(input=user_msg)
    call = model_registry.model_call(
        system=variant.prompt.system,
        user=rendered,
        generation=variant.generation,
        few_shots=variant.prompt.few_shots,
    )
    sql = _extract_sql(call.text)
    out = [f"SQL:\n{sql}\n"]

    # Try to execute if DB is configured.
    db_cfg_path = project_dir / "data" / "db.json"
    if db_cfg_path.exists():
        from . import db as db_mod
        cfg = db_mod.DBConfig.model_validate_json(db_cfg_path.read_text(encoding="utf-8"))
        res = db_mod.safe_execute(cfg, sql)
        if res.ok:
            out.append(_format_result_table(res.columns, res.rows[:10], truncated=res.truncated))
        else:
            out.append(f"[execution failed: {res.error_kind}] {res.error_message}")
    else:
        out.append("(no data/db.json — skipping execution)")
    out.append(f"({call.input_tokens}+{call.output_tokens} tok · ${call.cost_usd:.4f})")
    return "\n".join(out)


def _turn_single(variant: Variant, user_msg: str) -> str:
    """Fallback for capabilities without a dedicated chat handler."""
    rendered = variant.prompt.user_template.format(input=user_msg)
    call = model_registry.model_call(
        system=variant.prompt.system,
        user=rendered,
        generation=variant.generation,
        few_shots=variant.prompt.few_shots,
    )
    return f"{call.text}\n   ({call.input_tokens}+{call.output_tokens} tok · ${call.cost_usd:.4f})"


def _format_result_table(columns: list[str], rows: list[tuple], *, truncated: bool) -> str:
    """Tiny ASCII renderer. Doesn't pull in rich/tabulate to keep the
    REPL deployable in barebones environments."""
    if not rows:
        return "(0 rows)"
    str_rows = [tuple("" if v is None else str(v) for v in r) for r in rows]
    widths = [max(len(c), *(len(r[i]) for r in str_rows)) for i, c in enumerate(columns)]
    lines = []
    lines.append(" | ".join(c.ljust(widths[i]) for i, c in enumerate(columns)))
    lines.append("-+-".join("-" * w for w in widths))
    for r in str_rows:
        lines.append(" | ".join(r[i].ljust(widths[i]) for i in range(len(columns))))
    if truncated:
        lines.append(f"... (showing first {len(rows)} rows, more truncated)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def _print_banner(mission: Mission, variant: Variant) -> None:
    print("=" * 70)
    print(f"evalsmith chat · mission {mission.mission_id} ({mission.composition.task_modality})")
    print(f"variant {variant.variant_id} · model {variant.generation.model}")
    print(f"Type /help for commands · /exit to quit")
    print("=" * 70)


def _print_help() -> None:
    print(
        "Commands:\n"
        "  /help     — this message\n"
        "  /variant  — print the active variant config (prompt, model, retrieval)\n"
        "  /reset    — clear conversation memory (chatbot mode only)\n"
        "  /exit     — quit (transcript saved to results/chat_log_*.jsonl)"
    )
