# lib/capabilities/nlq.py
# Natural Language Query capability — NL question -> structured query
# (SQL, GraphQL, an API call, or a DSL fragment).
#
# Critical here is *tool_call_match* eval strategy: we compare the produced
# query against an expected canonical query rather than running it (which
# would require live DB access during eval). For projects that want
# execution-equivalence eval, the recipe can opt into running the query in
# a sandboxed read-only DB — see recipes/nlq_sql.json.

from __future__ import annotations
from pathlib import Path
from typing import Optional
import json

from .base import CapabilityBase, CapabilityContext, RunOutput
from .registry import register_capability
from ..schemas.variant import Variant
from ..schemas.eval_case import EvalCase
from .. import registry as model_registry


# Module-level cache of (project_dir -> DBConfig). Lets per-case execution
# avoid re-reading db.json from disk for every case in a trial. Cleared
# implicitly when the process exits.
_DB_CONFIG_CACHE: dict[str, object] = {}


def _load_db_config(project_dir: Optional[Path]):
    """Load <project>/data/db.json if present. Returns None if absent —
    the capability degrades to string-match eval, which is fine for
    tool_call_match missions."""
    if project_dir is None:
        return None
    key = str(project_dir)
    if key in _DB_CONFIG_CACHE:
        return _DB_CONFIG_CACHE[key]

    cfg_path = project_dir / "data" / "db.json"
    if not cfg_path.exists():
        _DB_CONFIG_CACHE[key] = None
        return None

    # Lazy import: lib.db has SQLAlchemy as a soft dependency.
    from .. import db as db_mod
    cfg = db_mod.DBConfig.model_validate_json(cfg_path.read_text(encoding="utf-8"))
    _DB_CONFIG_CACHE[key] = cfg
    return cfg


@register_capability("nlq_to_query")
class NlqCapability(CapabilityBase):
    """NL -> query. Variant.prompt.system holds the schema description /
    DSL guide; Variant.prompt.few_shots holds example (NL, query) pairs.

    Two eval modes:
      * tool_call_match (default) — generated SQL string compared to gold
                                    SQL string (whitespace+case normalized).
      * execution_equivalence     — run both queries against the project's
                                    DB (configured in data/db.json) and
                                    compare result sets. The latter is much
                                    more meaningful but requires DB access.

    The Strategist's main levers here are:
      * prompt_rewrite       — refine the schema description
      * few_shot_selection   — which examples best teach the patterns?
      * model_swap           — Haiku for simple SELECTs, Sonnet for joins
      * tool_schema_edit     — if we wrap the call in a structured tool
    """

    primary_metrics = ["exact_match_normalized", "execution_equivalence"]
    secondary_metrics = ["cost_usd", "p95_latency_ms", "syntactic_validity"]
    allowed_arms = [
        "prompt_rewrite",
        "few_shot_selection",
        "model_swap",
        "tool_schema_edit",
        "decoding_params",
    ]

    def run_single_case(self, variant: Variant, case: EvalCase, ctx: CapabilityContext) -> RunOutput:
        # 1. Generate SQL from the LLM. NLQ does not retrieve over a corpus,
        #    so variant.retrieval is intentionally ignored here.
        rendered = variant.prompt.user_template.format(input=case.input)
        call = model_registry.model_call(
            system=variant.prompt.system,
            user=rendered,
            generation=variant.generation,
            few_shots=variant.prompt.few_shots,
        )

        # 2. If the project has a db.json configured, execute the generated
        #    SQL safely + record the result set on the RunOutput. The
        #    execution_equivalence metric in lib/eval.py reads this to
        #    compare against the gold result.
        db_cfg = _load_db_config(ctx.project_dir)
        sql_text = _extract_sql(call.text)
        exec_payload = None
        if db_cfg is not None:
            from .. import db as db_mod
            actual = db_mod.safe_execute(db_cfg, sql_text)
            # Pack as a JSON-serializable dict so the RunOutput.tool_calls
            # field (which carries it for the metric to read) stays cheap.
            exec_payload = {
                "ok": actual.ok,
                "columns": actual.columns,
                "rows": [list(r) for r in actual.rows[:50]],   # cap for metric payload
                "n_rows": len(actual.rows),
                "error_kind": actual.error_kind,
                "error_message": actual.error_message,
                "truncated": actual.truncated,
                "generated_sql": sql_text,
            }

        return RunOutput(
            case_id=case.case_id,
            raw_output=sql_text,
            input_tokens=call.input_tokens,
            output_tokens=call.output_tokens,
            cost_usd=call.cost_usd,
            latency_ms=call.latency_ms,
            # We piggy-back on tool_calls to carry the exec result without
            # adding a new RunOutput field. Metrics know the convention.
            tool_calls=[exec_payload] if exec_payload else None,
        )


def _extract_sql(text: str) -> str:
    """Pull a SQL statement out of the LLM's response. LLMs frequently
    wrap SQL in ```sql ... ``` fences or add prose around it. We:
      1. Prefer the contents of a ```sql ... ``` block if present.
      2. Else prefer any ``` ... ``` block if present.
      3. Else return the whole response (let the safety guard reject if junk).
    """
    import re
    m = re.search(r"```sql\s*(.+?)```", text, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\s*(.+?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()
