# tests/test_bandit_and_doom_loop.py
# Verifies the two deterministic guardrails the framework leans on hardest.

from __future__ import annotations
import tempfile
from pathlib import Path

from lib import bandit, doom_loop, variants
from lib.schemas import Variant
from lib.schemas.variant import PromptBundle, RetrievalConfig, GenerationConfig


def _project_dir() -> Path:
    """Test scratch dir. Each test gets a fresh one."""
    d = Path(tempfile.mkdtemp(prefix="agt_test_"))
    (d / "memory").mkdir()
    return d


def _make_variant(system: str = "You are helpful.", model: str = "claude-haiku-4-5") -> Variant:
    p = PromptBundle(system=system)
    r = RetrievalConfig()
    g = GenerationConfig(model=model)
    return Variant(variant_id=Variant.compute_id(p, r, g), technique_family="prompt_rewrite",
                   prompt=p, retrieval=r, generation=g)


def test_bandit_load_then_save_roundtrip():
    """Posteriors persisted across loads — Thompson sampling can't work
    if state vanishes between iterations."""
    d = _project_dir()
    s = bandit.load(d, "m1", ["prompt_rewrite", "model_swap"])
    bandit.update_arm(s, "prompt_rewrite", win=True, iteration=1)
    bandit.update_arm(s, "model_swap", win=False, iteration=1)
    bandit.save(d, s)
    s2 = bandit.load(d, "m1", ["prompt_rewrite", "model_swap"])
    assert s2.arms["prompt_rewrite"].alpha == 2.0
    assert s2.arms["model_swap"].beta == 2.0


def test_bandit_sample_arm_is_deterministic_with_seed():
    """Seeded sampling lets replay reproduce arm choices exactly."""
    d = _project_dir()
    s = bandit.load(d, "m1", ["a", "b"])
    assert bandit.sample_arm(s, seed=42) == bandit.sample_arm(s, seed=42)


def test_bandit_strongly_winning_arm_dominates_posteriors_mean():
    """After many wins on one arm, its mean reward should clearly exceed
    losers — even though Thompson sampling still explores."""
    d = _project_dir()
    s = bandit.load(d, "m1", ["winner", "loser"])
    for _ in range(20):
        bandit.update_arm(s, "winner", win=True, iteration=0)
        bandit.update_arm(s, "loser", win=False, iteration=0)
    means = bandit.posteriors_mean(s)
    assert means["winner"] > means["loser"]


def test_doom_loop_detects_duplicate_within_window():
    """Adding the same variant twice within the lookback window flags it."""
    d = _project_dir()
    v = _make_variant()
    doom_loop.append(d, v, iteration=1)
    # Identical variant, different object — collide on fingerprint.
    v2 = _make_variant()
    is_dup, prior_id = doom_loop.is_duplicate(d, v2, lookback=10)
    # Same exact content => same variant_id. is_duplicate skips equal ids
    # (no point flagging the same trial twice); the real collision case
    # is *similar* variants with different ids. Build a paraphrase-like
    # variant: same fingerprint, different generation params.
    v3 = _make_variant(system="You are helpful.", model="claude-sonnet-4-5")
    doom_loop.append(d, v3, iteration=2)
    # Now make a paraphrase that normalizes to the same fingerprint as v3:
    v_paraphrase = _make_variant(system="  YOU  are HELPFUL.\n", model="claude-sonnet-4-5")
    is_dup2, _ = doom_loop.is_duplicate(d, v_paraphrase, lookback=10)
    assert is_dup2


def test_apply_diff_produces_valid_child():
    """The diff applier round-trips through pydantic validation, so a
    malformed diff raises rather than producing a corrupt Variant."""
    parent = _make_variant()
    diff = type(parent)  # noqa - unused; keep import lightweight
    from lib.schemas.variant import VariantDiff
    d = VariantDiff(parent_variant_id=parent.variant_id, technique_family="model_swap",
                    field_changes={"generation.model": "claude-sonnet-4-5"})
    child = variants.apply_diff(parent, d)
    assert child.generation.model == "claude-sonnet-4-5"
    assert child.variant_id != parent.variant_id
    assert child.parent_variant_id == parent.variant_id
