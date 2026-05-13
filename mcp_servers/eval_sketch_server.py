#!/usr/bin/env python
# mcp_servers/eval_sketch_server.py
#
# Stdio MCP server exposing the eval sketch query surface to subagents
# running inside Claude Code. The wire protocol is JSON-RPC 2.0 over
# stdin/stdout — minimal, dependency-free implementation so it works in
# any Python 3.10+ environment.
#
# Server identifies the project workspace via the GENAI_PROJECT_DIR env var;
# the Claude Code MCP config sets that per-launch.
#
# Tools exposed:
#   * eval_profile          — E1
#   * slice_performance     — E3
#   * failure_clusters      — E2
#   * retrieval_diagnostics — E4
#   * trace_structure       — E5
#   * cost_breakdown        — E6
#   * safety_incidents      — E7

from __future__ import annotations
import json
import os
import sys
from pathlib import Path

# We import the query functions from the library — single source of truth.
# This file just translates JSON-RPC calls into Python calls.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.sketch import queries  # noqa: E402


def _project_dir() -> Path:
    p = os.environ.get("GENAI_PROJECT_DIR")
    if not p:
        raise RuntimeError("GENAI_PROJECT_DIR env var not set; MCP server cannot resolve workspace.")
    return Path(p)


# ---------------------------------------------------------------------------
# Tool dispatch table — keys are tool names; values are callables that
# receive the kwargs dict from the JSON-RPC `params.arguments`.
# ---------------------------------------------------------------------------

TOOLS = {
    "eval_profile": lambda **kw: queries.eval_profile(_project_dir(), **kw),
    "slice_performance": lambda **kw: queries.slice_performance(_project_dir(), **kw),
    "failure_clusters": lambda **kw: queries.failure_clusters(_project_dir(), **kw),
    "retrieval_diagnostics": lambda **kw: queries.retrieval_diagnostics(_project_dir(), **kw),
    "trace_structure": lambda **kw: queries.trace_structure(_project_dir(), **kw),
    "cost_breakdown": lambda **kw: queries.cost_breakdown(_project_dir(), **kw),
    "safety_incidents": lambda **kw: queries.safety_incidents(_project_dir(), **kw),
}


# Tool input schemas — declared minimally; Claude Code reads these so the
# subagent knows the parameter shape without trial-and-error.
TOOL_SCHEMAS = {
    "eval_profile": {"type": "object", "properties": {}, "additionalProperties": False},
    "slice_performance": {
        "type": "object",
        "properties": {
            "trial_id": {"type": "string"},
            "slice_key": {"type": "string"},
            "metric_name": {"type": "string"},
            "top_n": {"type": "integer", "default": 20},
        },
    },
    "failure_clusters": {"type": "object", "properties": {"top_n": {"type": "integer", "default": 10}}},
    "retrieval_diagnostics": {"type": "object", "properties": {"trial_id": {"type": "string"}}},
    "trace_structure": {"type": "object", "properties": {"trial_id": {"type": "string"}}},
    "cost_breakdown": {"type": "object", "properties": {"last_n": {"type": "integer", "default": 5}}},
    "safety_incidents": {"type": "object", "properties": {"trial_id": {"type": "string"}}},
}


# ---------------------------------------------------------------------------
# JSON-RPC loop
# ---------------------------------------------------------------------------

def _send(obj: dict) -> None:
    """Frame a JSON-RPC message and write to stdout."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _handle(msg: dict) -> None:
    """Dispatch one JSON-RPC request."""
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}

    if method == "initialize":
        # Standard MCP handshake.
        _send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "eval_sketch_server", "version": "0.1.0"},
        }})
        return

    if method == "tools/list":
        _send({"jsonrpc": "2.0", "id": mid, "result": {
            "tools": [
                {"name": n, "description": f"Query sketch layer for {n}", "inputSchema": TOOL_SCHEMAS[n]}
                for n in TOOLS
            ]
        }})
        return

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = TOOLS.get(name)
        if fn is None:
            _send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown tool: {name}"}})
            return
        try:
            result = fn(**args)
            # MCP tools return content blocks; we serialize the result to text JSON.
            _send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": json.dumps(result, indent=2)}]
            }})
        except Exception as e:
            _send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": str(e)}})
        return

    # Notifications (no id) — ignore unknowns silently per JSON-RPC spec.
    if mid is None:
        return
    _send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown method: {method}"}})


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        _handle(msg)


if __name__ == "__main__":
    main()
