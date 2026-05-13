#!/usr/bin/env python
# mcp_servers/retrieval_server.py
#
# Cross-project knowledge retrieval over the framework root's
# `knowledge/` directory. The Strategist queries this to find relevant
# prompt patterns / RAG recipes from prior projects.
#
# Shares the same JSON-RPC stdio shape as eval_sketch_server.

from __future__ import annotations
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import retrieval as ret  # noqa: E402
from lib.schemas import Mission  # noqa: E402


def _mission() -> Mission:
    """Read the active Mission from GENAI_PROJECT_DIR/MISSION.json."""
    p = Path(os.environ["GENAI_PROJECT_DIR"]) / "MISSION.json"
    return Mission.model_validate_json(p.read_text(encoding="utf-8"))


TOOLS = {
    # Returns up to top_n_per_file relevant snippets for the active Mission.
    "load_snippets": lambda **kw: ret.load_snippets_for(_mission(), **kw),
}

TOOL_SCHEMAS = {
    "load_snippets": {
        "type": "object",
        "properties": {"top_n_per_file": {"type": "integer", "default": 5}},
    },
}


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _handle(msg: dict) -> None:
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if method == "initialize":
        _send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "retrieval_server", "version": "0.1.0"},
        }})
        return
    if method == "tools/list":
        _send({"jsonrpc": "2.0", "id": mid, "result": {
            "tools": [{"name": n, "description": f"knowledge: {n}", "inputSchema": TOOL_SCHEMAS[n]} for n in TOOLS]
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
            _send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}})
        except Exception as e:
            _send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": str(e)}})


def main():
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
