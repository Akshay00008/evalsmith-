# lib/schemas/plan.py
# Inter-agent proposals. These are the JSON payloads agents drop into
# `memory/agent_inbox/iter_NNNN/` for each other. Each is a separate
# pydantic class so the Auditor (a deterministic Python module, not an LLM)
# can validate them without re-prompting an LLM.

from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field

from .variant import VariantDiff


class PriorEvidence(BaseModel):
    """Every Strategist proposal must cite *why* this variant is worth
    trying. The Sentinel & Auditor refuse proposals without this. In
    breakthrough mode `kind` must be `domain_prior` with a URL.
    """
    kind: Literal["sketch_query", "prior_trial", "seed_hypothesis", "domain_prior", "knowledge_library"]
    # Reference id depending on kind:
    #   sketch_query     -> the query string + result hash
    #   prior_trial      -> trial_id
    #   seed_hypothesis  -> seed id from seeds/universal_seeds.jsonl
    #   domain_prior     -> URL / arxiv / DOI
    #   knowledge_library-> pattern id from knowledge/prompt_pattern_library.jsonl
    reference: str
    # One-sentence justification — read by the Inspector for prose, not
    # used by automated logic.
    note: str = ""


class StrategistProposal(BaseModel):
    """What the Strategist subagent emits per iteration."""
    iteration: int
    mission_id: str
    diff: VariantDiff
    prior_evidence: PriorEvidence
    # Expected metric delta on the primary criterion. The Auditor uses
    # this to detect over-claiming (predicted +20% improvement when the
    # technique family historically delivers +2%).
    predicted_delta: Optional[float] = None
    # The bandit arm chosen — also recorded so we can update posteriors
    # after the trial. Should match diff.technique_family but stored
    # explicitly for replay clarity.
    arm: str


class AuditorVerdict(BaseModel):
    """The Auditor's verdict on a StrategistProposal. ACCEPT means proceed
    to Operator; WARN means proceed but flag in the synthesis; FAIL means
    return to Strategist for another proposal; CATASTROPHIC means terminate
    the entire run (e.g. eval contamination detected)."""
    iteration: int
    proposal_variant_id: Optional[str]    # may be None if proposal was malformed
    verdict: Literal["ACCEPT", "WARN", "FAIL", "CATASTROPHIC"]
    # Each check that ran, with its status. Lets the Inspector cite specific
    # checks in the synthesis prose.
    check_results: list["CheckResult"] = Field(default_factory=list)
    # If the verdict is WARN, the Operator still runs but the Curator must
    # downgrade the confidence tier in FINAL.md.
    warn_reason: Optional[str] = None
    fail_reason: Optional[str] = None


class CheckResult(BaseModel):
    """One deterministic check executed by the Auditor (e.g. 'eval
    contamination', 'cost projection', 'judge calibration')."""
    name: str
    passed: bool
    detail: str = ""
    # Severity used to compute the overall verdict. The verdict is the
    # max severity of any check.
    severity: Literal["ok", "warn", "fail", "catastrophic"] = "ok"


# Pydantic v2 needs explicit rebuild for forward refs across classes.
AuditorVerdict.model_rebuild()
