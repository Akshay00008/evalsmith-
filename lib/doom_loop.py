# lib/doom_loop.py
# Disk-backed fingerprint history. The framework appends a normalized
# fingerprint to RECENT_PLANS.jsonl after every accepted proposal. The
# Sentinel subagent consults this file before approving a new proposal —
# if a structurally-identical variant has been tried recently, reject it
# and force the Strategist back to the drawing board.
#
# We tune the lookback window in `is_duplicate` rather than truncating the
# file (replay needs the full history).

from __future__ import annotations
from pathlib import Path
from typing import Optional, Tuple
import json
import time

from .variants import normalized_fingerprint
from .schemas.variant import Variant


def append(project_dir: Path, variant: Variant, *, iteration: int) -> str:
    """Append a fingerprint entry. Returns the fingerprint string."""
    fp = normalized_fingerprint(variant)
    path = project_dir / "memory" / "RECENT_PLANS.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "fingerprint": fp,
            "variant_id": variant.variant_id,
            "iteration": iteration,
            "ts": time.time(),
        }) + "\n")
    return fp


def is_duplicate(project_dir: Path, variant: Variant, *, lookback: int = 10) -> Tuple[bool, Optional[str]]:
    """Check if this variant collides with one tried in the last `lookback`
    iterations. Returns (is_duplicate, prior_variant_id).

    The lookback is a window because two identical fingerprints far apart
    in time can be legitimate (e.g. reverting to a known-good config in
    breakthrough mode). Within a short window it almost always indicates
    the Strategist looping.
    """
    fp = normalized_fingerprint(variant)
    path = project_dir / "memory" / "RECENT_PLANS.jsonl"
    if not path.exists():
        return False, None
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    # Look only at the most recent `lookback` entries.
    for row in reversed(rows[-lookback:]):
        if row["fingerprint"] == fp and row["variant_id"] != variant.variant_id:
            return True, row["variant_id"]
    return False, None
