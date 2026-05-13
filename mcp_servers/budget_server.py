#!/usr/bin/env python
# mcp_servers/budget_server.py
#
# Read-only budget query surface. Subagents (especially the Strategist
# and Auditor) need to know "how much budget is left?" without parsing the
# ledger themselves.

from __future__ import annotations
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import budget  # noqa: E402
from lib.schemas import Mission  # noqa: E402


def _project_dir() -> Path:
    return Path(os.environ["GENAI_PROJECT_DIR"])


def _budget_status(**kw) -> dict:
    mission = Mission.model_validate_json((_project_dir() / "MISSION.json").read_text(encoding="utf-8"))
    tracker = budget.BudgetTracker(_project_dir())
    return {
        "mission_id": mission.mission_id,
        "spent_usd": tracker.spent,
        "ceiling_usd": mission.total_budget_usd,
        "remaining_usd": tracker.remaining(mission.total_budget_usd),
        "pct_used": tracker.spent / mission.total_budget_usd if mission.total_budget_usd else 1.0,
    }


def _cost_projection(*, model: str, input_tokens: int, output_tokens: int, n_cases: int = 1) -> dict:
    """Project the cost of running a hypothetical trial. Strategist uses
    this before proposing model_swap arms."""
    from lib.registry import estimate_cost
    per_case = estimate_cost(model, input_tokens, output_tokens)
    return {"per_case_usd": per_case, "total_usd": per_case * n_cases}


TOOLS = {
    "budget_status": _budget_status,
    "cost_projection": _cost_projection,
}

TOOL_SCHEMAS = {
    "budget_status": {"type": "object", "properties": {}},
    "cost_projection": {
        "type": "object",
        "properties": {
            "model": {"type": "string"},
            "input_tokens": {"type": "integer"},
            "output_tokens": {"type": "integer"},
            "n_cases": {"type": "integer", "default": 1},
        },
        "required": ["model", "input_tokens", "output_tokens"],
    },
}


def _send(obj): sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()


def _handle(msg):
    mid = msg.get("id"); method = msg.get("method"); params = msg.get("params") or {}
    if method == "initialize":
        _send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
            "serverInfo": {"name": "budget_server", "version": "0.1.0"}}})
        return
    if method == "tools/list":
        _send({"jsonrpc": "2.0", "id": mid, "result": {
            "tools": [{"name": n, "description": f"budget: {n}", "inputSchema": TOOL_SCHEMAS[n]} for n in TOOLS]}})
        return
    if method == "tools/call":
        name = params.get("name"); args = params.get("arguments") or {}
        fn = TOOLS.get(name)
        if fn is None:
            _send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown tool: {name}"}}); return
        try:
            result = fn(**args)
            _send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}})
        except Exception as e:
            _send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": str(e)}})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        try: msg = json.loads(line)
        except json.JSONDecodeError: continue
        _handle(msg)


if __name__ == "__main__":
    main()
