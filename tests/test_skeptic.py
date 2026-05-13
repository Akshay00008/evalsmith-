# tests/test_skeptic.py
# Verifies that the Auditor's check set produces the expected severity
# on the canonical failure shapes — eval contamination is the marquee
# case (any leak there should halt the run).

from __future__ import annotations
import tempfile
from pathlib import Path

from lib import skeptic
from lib.schemas import (
    Mission, MissionTuple, SuccessCriterion,
    EvalCase, EvalSet,
    Variant, VariantDiff,
    StrategistProposal, PriorEvidence,
)
from lib.schemas.variant import PromptBundle, RetrievalConfig, GenerationConfig


def _basic_mission(es: EvalSet) -> Mission:
    comp = MissionTuple(task_modality="rag_qa", eval_strategy="exact_match")
    return Mission(
        mission_id=Mission.compute_id("p", comp, es.content_hash(), "g"),
        project_name="p",
        framework_version="0.1.0",
        goal_prose="g",
        composition=comp,
        success_criteria=[SuccessCriterion(metric="exact_match_normalized", target=0.8, operational_floor=0.6, is_primary=True)],
        eval_set_hash=es.content_hash(),
        total_budget_usd=50.0,
    )


def _basic_eval_set(extra_input: str = "what's the refund policy?") -> EvalSet:
    cases = [
        EvalCase(case_id=f"c{i:03d}", input=f"baseline question {i} with enough length", expected=f"a{i}", tags=["t"])
        for i in range(25)
    ]
    cases.append(EvalCase(case_id="c_contam", input=extra_input, expected="ans", tags=["t"]))
    return EvalSet(eval_set_id="es", cases=cases)


def _proposal(field_changes=None, prior_ref="seed:u01") -> StrategistProposal:
    # Use an explicit None sentinel — `or {default}` would replace an
    # intentionally-empty dict with the default, hiding the test case.
    fc = {"prompt.system": "be helpful"} if field_changes is None else field_changes
    return StrategistProposal(
        iteration=1,
        mission_id="x" * 16,
        diff=VariantDiff(
            parent_variant_id="parent_id_abcd",
            technique_family="prompt_rewrite",
            field_changes=fc,
        ),
        prior_evidence=PriorEvidence(kind="seed_hypothesis", reference=prior_ref),
        arm="prompt_rewrite",
    )


def _variant(system: str = "You are helpful.") -> Variant:
    p = PromptBundle(system=system, user_template="{input}")
    r = RetrievalConfig()
    g = GenerationConfig()
    return Variant(
        variant_id=Variant.compute_id(p, r, g),
        technique_family="prompt_rewrite",
        prompt=p, retrieval=r, generation=g,
    )


def test_auditor_accepts_clean_proposal():
    es = _basic_eval_set()
    m = _basic_mission(es)
    v = _variant()
    prop = _proposal()
    verdict = skeptic.audit_proposal(
        mission=m, proposal=prop, variant_full=v,
        eval_set=es, prior_trials=[], judge_calibration=None,
        project_dir=Path(tempfile.mkdtemp()),
    )
    assert verdict.verdict == "ACCEPT"


def test_auditor_fails_missing_prior_evidence():
    es = _basic_eval_set()
    m = _basic_mission(es)
    v = _variant()
    prop = _proposal(prior_ref="")  # missing reference -> FAIL
    verdict = skeptic.audit_proposal(
        mission=m, proposal=prop, variant_full=v,
        eval_set=es, prior_trials=[], judge_calibration=None,
        project_dir=Path(tempfile.mkdtemp()),
    )
    assert verdict.verdict == "FAIL"


def test_auditor_catastrophic_on_eval_contamination():
    """The critical one: if an eval question is embedded in the prompt,
    the run must halt — not warn, not retry, halt."""
    contamination_text = "what's the refund policy for digital products and is it different from physical?"
    es = _basic_eval_set(extra_input=contamination_text)
    m = _basic_mission(es)
    # Embed the eval question into the system prompt verbatim.
    v = _variant(system=f"You answer questions like: {contamination_text}")
    prop = _proposal()
    verdict = skeptic.audit_proposal(
        mission=m, proposal=prop, variant_full=v,
        eval_set=es, prior_trials=[], judge_calibration=None,
        project_dir=Path(tempfile.mkdtemp()),
    )
    assert verdict.verdict == "CATASTROPHIC"


def test_auditor_fails_empty_diff():
    es = _basic_eval_set()
    m = _basic_mission(es)
    v = _variant()
    prop = _proposal(field_changes={})
    verdict = skeptic.audit_proposal(
        mission=m, proposal=prop, variant_full=v,
        eval_set=es, prior_trials=[], judge_calibration=None,
        project_dir=Path(tempfile.mkdtemp()),
    )
    assert verdict.verdict == "FAIL"
