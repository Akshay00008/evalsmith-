# tests/test_end_to_end_stub.py
# End-to-end smoke test using the stub backend (no API key required).
# Runs: build sketch -> execute a trial -> sketch update -> finalize.
# If this passes, the framework is wired up correctly across all
# subsystems even though no real LLM was called.

from __future__ import annotations
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _force_stub_backend(monkeypatch):
    """Make sure no test accidentally hits a real backend."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def test_full_pipeline_stub():
    """Build sketch -> execute trial -> finalize. Stub backend throughout."""
    from lib.schemas import (
        Mission, MissionTuple, SuccessCriterion,
        EvalCase, EvalSet,
        Variant,
    )
    from lib.schemas.variant import PromptBundle, RetrievalConfig, GenerationConfig
    from lib.schemas.state import RunState
    from lib import sketch, run as run_mod, finalize
    # Important: importing lib.capabilities triggers the @register_capability
    # decorators that populate the registry.
    from lib import capabilities  # noqa: F401

    project_dir = Path(tempfile.mkdtemp(prefix="agt_e2e_"))
    (project_dir / "memory").mkdir()
    (project_dir / "results").mkdir()

    # Build a small eval set.
    cases = [
        EvalCase(case_id=f"c{i:03d}", input=f"question {i}", expected=f"answer {i}", tags=["t"])
        for i in range(15)  # >=10 so bootstrap CI fires
    ]
    es = EvalSet(eval_set_id="e1", cases=cases)

    # Build the mission.
    comp = MissionTuple(task_modality="rag_qa", eval_strategy="exact_match")
    m = Mission(
        mission_id=Mission.compute_id("p", comp, es.content_hash(), "g"),
        project_name="p",
        framework_version="0.1.0",
        goal_prose="g",
        composition=comp,
        success_criteria=[SuccessCriterion(metric="exact_match_normalized", target=0.99, operational_floor=0.5, is_primary=True)],
        eval_set_hash=es.content_hash(),
        total_budget_usd=10.0,
    )
    (project_dir / "MISSION.json").write_text(m.model_dump_json(indent=2), encoding="utf-8")

    # Build the sketch.
    sketch.build_sketch(project_dir, m.mission_id, es)
    assert (project_dir / "sketch" / "e1_profile.json").exists()
    assert (project_dir / "sketch" / "manifest.json").exists()

    # Build a Variant + run a trial.
    p_bundle = PromptBundle(system="be helpful", user_template="Q: {input}")
    r = RetrievalConfig()
    g = GenerationConfig(model="claude-haiku-4-5")
    v = Variant(
        variant_id=Variant.compute_id(p_bundle, r, g),
        technique_family="prompt_rewrite",
        prompt=p_bundle, retrieval=r, generation=g,
    )
    trial = run_mod.execute_trial(
        project_dir=project_dir, mission=m, variant=v, eval_set=es,
        iteration=1, seed=0,
    )

    # Trial invariants.
    assert trial.mission_id == m.mission_id
    assert trial.variant_id == v.variant_id
    assert trial.metrics, "Expected at least one metric"
    assert trial.total_cost_usd >= 0
    # The append happened to the log.
    log_path = project_dir / "experiment_log.jsonl"
    assert log_path.exists()
    assert log_path.stat().st_size > 0
    # Sketch was updated (E6 cost row at minimum).
    assert (project_dir / "sketch" / "e6_cost.jsonl").stat().st_size > 0

    # Finalize.
    rs = RunState(mission_id=m.mission_id, current_iteration=1, terminated=True, terminated_reason="iteration_cap")
    rec = finalize.assemble_recommendation(
        project_dir=project_dir, mission=m, run_state=rs, log=[trial], judge_calibration=None,
    )
    assert rec.confidence in ("high", "medium", "low", "no_signal")
    final_md = finalize.write_final_md(project_dir, rec, m)
    assert final_md.exists()
    assert final_md.read_text(encoding="utf-8").startswith("# FINAL")
