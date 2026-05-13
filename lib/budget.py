# lib/budget.py
# Append-only budget ledger. Mirrors `experiment_log.jsonl` in shape: each
# row is one event. Reconciling the ledger should always agree with the
# sum of TrialResult.total_cost_usd — if it doesn't, something bypassed
# lib/registry.py.

from __future__ import annotations
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Literal, Optional
import json
import time


class BudgetLedgerEntry(BaseModel):
    """One charge against the Mission budget."""
    entry_id: str
    mission_id: str
    iteration: int
    timestamp_unix: float = Field(default_factory=time.time)
    kind: Literal["trial", "judge", "embed", "rerank", "retrieval", "redteam", "init"] = "trial"
    # Reference to the artifact that incurred the cost (trial_id, judge_id,
    # etc.). Lets us pinpoint expensive components when over budget.
    ref_id: str = ""
    amount_usd: float
    note: str = ""


class BudgetTracker:
    """Stateful wrapper over the ledger file. The orchestrator instantiates
    one of these at the top of /run and consults `.spent` between iterations.

    The tracker is intentionally a thin layer — the canonical state lives in
    the file. /resume reads the file from scratch rather than relying on
    in-memory state.
    """

    def __init__(self, project_dir: Path):
        self._path = project_dir / "budget.jsonl"
        self._path.touch(exist_ok=True)
        self._spent: Optional[float] = None

    @property
    def spent(self) -> float:
        """Sum of all entries. Cached after the first read; invalidate via
        record() which updates the cache incrementally."""
        if self._spent is None:
            self._spent = 0.0
            with self._path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self._spent += BudgetLedgerEntry.model_validate_json(line).amount_usd
        return self._spent

    def record(self, entry: BudgetLedgerEntry) -> None:
        """Append an entry. The ledger is the source of truth — never
        mutate prior entries; corrections are new entries with negative
        amount_usd and a note explaining the adjustment."""
        with self._path.open("a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")
        # Keep the in-memory cache in sync rather than re-reading.
        self._spent = (self._spent or 0.0) + entry.amount_usd

    def remaining(self, ceiling_usd: float) -> float:
        """Budget left given a ceiling. Negative means over budget."""
        return ceiling_usd - self.spent
