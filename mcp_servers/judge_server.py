#!/usr/bin/env python
# mcp_servers/judge_server.py
#
# Exposes judge calibration data to subagents. The Auditor and Curator
# both need to inspect the latest JudgeCalibration; rather than open the
# file directly, they go through this server so the access pattern is
# auditable and replayable.

from __future__ import annotations
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import judges  # noqa: E402


def _project_dir() -> Path:
    return Path(os.environ["GENAI_PROJECT_DIR"])


def _latest_calibration(**_kw) -> dict:
    c = judges.latest_calibration(_project_dir())
    return c.model_dump() if c else {}


TOOLS = {"latest_calibration": _latest_calibration}
TOOL_SCHEMAS = {"latest_calibration": {"type": "object", "properties": {}}}


def _send(obj): sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()


def _handle(msg):
    mid = msg.get("id"); method = msg.get("method"); params = msg.get("params") or {}
    if method == "initialize":
        _send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
            "serverInfo": {"name": "judge_server", "version": "0.1.0"}}})
        return
    if method == "tools/list":
        _send({"jsonrpc": "2.0", "id": mid, "result": {
            "tools": [{"name": n, "description": f"judge: {n}", "inputSchema": TOOL_SCHEMAS[n]} for n in TOOLS]}})
        return
    if method == "tools/call":
        name = params.get("name"); args = params.get("arguments") or {}
        fn = TOOLS.get(name)
        if fn is None:
            _send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown tool: {name}"}}); return
        try:
            _send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(fn(**args), indent=2)}]}})
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
