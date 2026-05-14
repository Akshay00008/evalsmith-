# tests/test_eval_gen.py
# Verify the LLM-driven eval-set generator end-to-end against the stub
# backend. Stub mode returns deterministic placeholder text so the
# generator's JSON parser will always fall through to the "could not
# parse" path — which is fine: we want to verify the pipeline (sampling,
# parsing, red-team padding, file write) works regardless of LLM output
# quality.

from __future__ import annotations
import json
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _force_stub_backend(monkeypatch):
    """Strip API keys so registry.py routes to deterministic stub mode."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def _scratch_project_with_corpus(rows: list[dict]) -> Path:
    """Build a temp project workspace with a populated corpus.jsonl."""
    pd = Path(tempfile.mkdtemp(prefix="evgen_"))
    (pd / "data").mkdir()
    with (pd / "data" / "corpus.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    # Clear the corpus loader's cache so we read the fresh tmp file.
    from lib import corpus
    corpus._load_corpus.cache_clear()
    return pd


def test_generate_from_corpus_returns_result_with_redteam_padding():
    """Even in stub mode (where the LLM output won't parse as JSON), the
    generator should return a GenerationResult populated with the
    built-in red-team cases — eval sets always need refusal coverage."""
    from lib import eval_gen

    project = _scratch_project_with_corpus([
        # Long enough chunks to clear the 200-char eligibility floor.
        {"doc_id": f"d{i}", "text": "This is a substantive passage about refunds and policies. " * 8}
        for i in range(5)
    ])

    result = eval_gen.generate_from_corpus(project, n_cases=3, model="claude-haiku-4-5")
    assert result.n_attempted == 3
    # Stub mode: parser fails -> no auto cases. But red-team padding adds 3.
    assert len(result.cases) >= 3
    # Every case in the result should be a valid EvalCase.
    from lib.schemas import EvalCase
    for c in result.cases:
        assert isinstance(c, EvalCase)
    # Red-team cases are tagged.
    redteam_ids = {c.case_id for c in result.cases if "redteam" in (c.tags or [])}
    assert len(redteam_ids) == 3


def test_generate_skips_short_chunks():
    """Chunks under 200 chars are ineligible. With only short chunks the
    generator returns an empty result + a clear warning."""
    from lib import eval_gen
    project = _scratch_project_with_corpus([
        {"doc_id": "d1", "text": "tiny."},
        {"doc_id": "d2", "text": "also short."},
    ])
    result = eval_gen.generate_from_corpus(project, n_cases=5, include_redteam=False)
    assert result.cases == []
    assert result.warnings, "Expected at least one warning"
    assert "too short" in result.warnings[0].lower()


def test_generate_with_no_corpus_returns_empty_with_warning():
    """No corpus.jsonl at all → empty cases + warning."""
    from lib import eval_gen
    project = Path(tempfile.mkdtemp())
    (project / "data").mkdir()
    # Clear cache so the (missing) file is re-checked.
    from lib import corpus
    corpus._load_corpus.cache_clear()
    result = eval_gen.generate_from_corpus(project, n_cases=5)
    assert result.cases == []
    assert "ingest pdfs" in " ".join(result.warnings).lower()


def test_write_eval_set_round_trips():
    """write_eval_set writes valid JSONL that can be re-read into EvalCases."""
    from lib import eval_gen
    from lib.schemas import EvalCase
    project = Path(tempfile.mkdtemp())
    (project / "data").mkdir()

    cases = [
        EvalCase(case_id="t1", input="What is X?", expected="X is Y.", tags=["a"]),
        EvalCase(case_id="t2", input="What is Z?", expected=None, tags=["should_refuse"]),
    ]
    n = eval_gen.write_eval_set(project, cases, append=False)
    assert n == 2

    # Re-read.
    out_path = project / "data" / "eval_set.jsonl"
    lines = [line for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 2
    parsed = [EvalCase.model_validate_json(line) for line in lines]
    assert parsed[0].case_id == "t1"
    assert parsed[1].expected is None


def test_write_eval_set_append_preserves_existing():
    from lib import eval_gen
    from lib.schemas import EvalCase
    project = Path(tempfile.mkdtemp())
    (project / "data").mkdir()
    first = [EvalCase(case_id="a", input="q1", expected="a1", tags=[])]
    second = [EvalCase(case_id="b", input="q2", expected="a2", tags=[])]

    eval_gen.write_eval_set(project, first, append=False)
    eval_gen.write_eval_set(project, second, append=True)
    out_path = project / "data" / "eval_set.jsonl"
    n = sum(1 for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip())
    assert n == 2


def test_parse_json_blob_handles_fenced_and_unfenced():
    """The JSON extractor should tolerate both code-fenced and bare LLM output."""
    from lib.eval_gen import _parse_json_blob
    assert _parse_json_blob('{"question": "q", "answer": "a"}') == {"question": "q", "answer": "a"}
    assert _parse_json_blob('```json\n{"q": 1}\n```') == {"q": 1}
    assert _parse_json_blob('Here is the JSON:\n{"q": 1, "a": 2}\nDone.') == {"q": 1, "a": 2}
    assert _parse_json_blob("not json at all") is None
    assert _parse_json_blob("") is None
