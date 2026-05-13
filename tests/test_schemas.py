# tests/test_schemas.py
# Schema-level invariants. If these fail, every other test is meaningless.

from __future__ import annotations
import pytest

from lib.schemas import (
    Mission, MissionTuple, SuccessCriterion,
    EvalCase, EvalSet,
    Variant, VariantDiff,
    PromptBundle, RetrievalConfig, GenerationConfig,
)
from lib.schemas.variant import PromptBundle as _PB, RetrievalConfig as _RC, GenerationConfig as _GC


def _make_eval_set(n: int = 25) -> EvalSet:
    """Helper — n synthetic cases. n=25 sneaks past the eval-set-size floor."""
    return EvalSet(
        eval_set_id="t1",
        cases=[
            EvalCase(case_id=f"c{i:03d}", input=f"question {i}", expected="answer", tags=["t1"])
            for i in range(n)
        ],
    )


def test_eval_set_content_hash_is_stable():
    """Two eval sets with the same cases (any order) hash identically.
    Critical because Mission.eval_set_hash pins this."""
    es1 = _make_eval_set()
    es2 = EvalSet(eval_set_id="t2", cases=list(reversed(es1.cases)))
    assert es1.content_hash() == es2.content_hash()


def test_mission_id_is_deterministic():
    """Same inputs to Mission.compute_id must yield the same id, otherwise
    re-running /plan orphans prior artifacts."""
    comp = MissionTuple(task_modality="rag_qa", eval_strategy="judge_llm")
    h = "deadbeef00000000"
    a = Mission.compute_id("p1", comp, h, "find best RAG")
    b = Mission.compute_id("p1", comp, h, "find best RAG")
    assert a == b
    assert len(a) == 16


def test_mission_id_validation_rejects_garbage():
    """The validator catches callers that pass random strings."""
    comp = MissionTuple(task_modality="rag_qa", eval_strategy="judge_llm")
    with pytest.raises(ValueError):
        Mission(
            mission_id="not-hex!",
            project_name="p",
            framework_version="0.1.0",
            goal_prose="g",
            composition=comp,
            success_criteria=[SuccessCriterion(metric="judge_score", target=0.8, operational_floor=0.6)],
            eval_set_hash="abc",
        )


def test_variant_id_is_content_hash():
    """Two variants with identical content collide intentionally — that's
    what the doom-loop check exploits."""
    p = PromptBundle(system="hi", user_template="{input}")
    r = RetrievalConfig()
    g = GenerationConfig()
    id1 = Variant.compute_id(p, r, g)
    id2 = Variant.compute_id(p, r, g)
    assert id1 == id2


def test_variant_id_differs_on_change():
    p1 = PromptBundle(system="A", user_template="{input}")
    p2 = PromptBundle(system="B", user_template="{input}")
    r = RetrievalConfig()
    g = GenerationConfig()
    assert Variant.compute_id(p1, r, g) != Variant.compute_id(p2, r, g)


def test_primary_criterion_falls_back_to_first():
    """If no criterion is marked primary, the first declared one drives."""
    comp = MissionTuple(task_modality="chatbot", eval_strategy="judge_llm")
    es = _make_eval_set()
    m = Mission(
        mission_id=Mission.compute_id("p", comp, es.content_hash(), "g"),
        project_name="p",
        framework_version="0.1.0",
        goal_prose="g",
        composition=comp,
        success_criteria=[
            SuccessCriterion(metric="judge_score", target=0.8, operational_floor=0.6),
            SuccessCriterion(metric="cost_usd", operator="<=", target=0.1, operational_floor=0.5),
        ],
        eval_set_hash=es.content_hash(),
    )
    assert m.primary_criterion().metric == "judge_score"
