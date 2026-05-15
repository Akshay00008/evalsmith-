# evalsmith — Team Onboarding Guide

> For developers, MLEs, and data engineers who want to understand the system and run it themselves.
> Time to first experiment: ~15 minutes via Docker, ~25 minutes via CLI.

---

## 1. What this actually is

evalsmith is a **multi-agent optimization loop** for GenAI applications. Instead of manually tweaking prompts and eyeballing outputs, you define a metric goal (`judge_score >= 0.85`) and let the framework systematically mutate prompt/retrieval/model config, score each variant against a fixed eval set, and recommend the winner with a confidence tier.

Six things worth knowing upfront before you touch any code:

| Concept | What it means in practice |
|---|---|
| **Capability** | The type of app you're optimizing: `rag_qa`, `nlq_to_query`, `research_agent`, `insight_agent`, `chatbot`, `search_engine` |
| **Variant** | A specific (system-prompt, model, retrieval-config, decoding-params) bundle — pydantic-validated, content-hashed |
| **TrialResult** | The output of running a Variant against your entire eval set — metrics + CIs + failure modes |
| **Bandit arm** | A technique family the optimizer picks via Thompson sampling: `prompt_rewrite`, `model_swap`, `chunking_change`, etc. |
| **Eval & Trace Sketch** | 7 compact JSON-Lines layers the agents query instead of reading raw eval data — keeps agent context lean |
| **Stub mode** | If no API key is set, `lib/registry.py` returns deterministic hash-based outputs — so you can run the full pipeline without spending a cent |

---

## 2. Get it running in 5 minutes (Docker path — recommended for team demos)

```bash
git clone https://github.com/AkshayDat/evalsmith-.git
cd evalsmith-

# Create your .env — copy the template and add your Anthropic key
cp .env.example .env
# Edit .env:  ANTHROPIC_API_KEY=sk-ant-api03-...
# Without a key it runs in stub mode (no real LLM calls, but the pipeline still runs)

docker compose up --build
# → open http://localhost:8000
```

The Docker image excludes `webui/` (Streamlit) to stay lean — the FastAPI UI is the container surface.
Project files persist in `./projects/` via bind-mount, so they survive `docker compose down`.

---

## 3. Get it running locally (CLI + Python path — recommended for development)

```bash
git clone https://github.com/AkshayDat/evalsmith-.git
cd evalsmith-

python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate

# Install everything (or cherry-pick extras — see pyproject.toml [project.optional-dependencies])
pip install -r requirements-full.txt
pip install -e .

# Set your API key (or skip for stub mode)
export ANTHROPIC_API_KEY=sk-ant-api03-...    # or add to .env and: pip install python-dotenv

# Verify the install
python -m pytest tests/ -v          # all tests should pass with no API key needed
python tools/audit_repo.py          # validates capabilities, recipes, schemas, knowledge files
```

---

## 4. End-to-end workflow — two surfaces

### 4a. Web UI (Docker or Streamlit)

Seven screens in order:

```
Dashboard → New Project → Upload Data → Mission → Run → Results → Chat
```

1. **New Project** — pick a capability (e.g. "Question answering over my documents"), give it a name
2. **Upload Data** — upload an eval set (CSV/XLSX/JSONL with `case_id` + `input` columns), optionally upload PDFs for RAG
3. **Mission** — set the target metric and budget (`judge_score >= 0.80`, `$50 budget`, `15 iterations`)
4. **Run** — click Start → watch live SSE stream of per-iteration events
5. **Results** — read `FINAL.md` (confidence tier + counterfactual delta + retraction conditions)
6. **Chat** — interactive REPL using the winning variant

> The web UI uses the **headless optimizer** (`lib/headless_optimizer.py`) — a deterministic template-based Strategist that runs without Claude Code.

### 4b. Claude Code (slash commands)

The real UX — subagents drive the loop autonomously. Requires Claude Code installed and MCP servers wired up:

```bash
# 1. Wire up MCP servers (copy template, set project dir)
cp .claude/settings.example.json .claude/settings.json
# Edit: set GENAI_PROJECT_DIR to projects/<your-project>

# 2. Run the pipeline in Claude Code
/init my_project       # profiles eval set, runs baseline trial
/plan my_project       # Architect Q&A, locks MISSION.json
/run my_project        # autonomous loop (Strategist → Sentinel → Auditor → Operator → Curator)
/status my_project     # safe to run anytime, read-only snapshot
/contribute my_project # anonymise + merge findings into cross-project knowledge library
```

---

## 5. Key files to read (in this order)

```
lib/schemas/variant.py          ← Variant/VariantDiff — the unit of optimization
lib/schemas/mission.py          ← Mission — what success means
lib/schemas/trial.py            ← TrialResult — what gets written after every run
lib/registry.py                 ← ALL LLM/embed calls go through here — swap vendor here
lib/headless_optimizer.py       ← the optimization loop used by the web UI
lib/run.py                      ← Trial executor — runs eval cases, writes log + sketch
lib/skeptic.py                  ← Auditor's deterministic checks (prior_evidence, cost, contamination)
lib/bandit.py                   ← Thompson sampling over technique families
lib/doom_loop.py                ← SHA-256 fingerprint novelty check (blocks paraphrase repeats)
lib/finalize.py                 ← Builds FinalRecommendation + FINAL.md
lib/capabilities/base.py        ← CapabilityBase — extend this to add a new capability
```

If you prefer top-down, start with `lib/headless_optimizer.py:run_optimization()` — it's the 300-line loop that calls everything else and is easy to trace.

---

## 6. Prepare your own eval set

The minimum required shape for `data/eval_set.jsonl`:

```jsonl
{"case_id": "q001", "input": "What is the refund policy?", "expected": "30 days no questions asked"}
{"case_id": "q002", "input": "How do I cancel my subscription?"}
```

**Required columns:** `case_id`, `input`
**Optional:** `expected` (for exact-match eval), `tags` (for slice-perf analysis), `relevant_doc_ids` (for retrieval recall)

**20 cases minimum** — the Architect refuses smaller eval sets.

### Auto-generate an eval set from your PDFs

If you have PDFs but no eval set yet:

1. Upload your PDFs via the **Upload Data** screen → ingest them (chunks → `data/corpus.jsonl`)
2. In the **Auto-generate** section, click **Generate from PDFs** — the LLM reads chunk samples and writes Q+A pairs
3. Review in the preview table, then **Save** or **Append**

Or via CLI:
```bash
python tools/ingest_pdfs.py --project projects/my_project   # PDFs → corpus.jsonl
python -c "
from lib.eval_gen import generate_from_corpus, write_eval_set
from pathlib import Path
result = generate_from_corpus(Path('projects/my_project'), n_cases=30, model='claude-haiku-4-5')
write_eval_set(Path('projects/my_project'), result.cases)
print(f'Generated {len(result.cases)} cases, cost: \${result.cost_usd:.4f}')
"
```

---

## 7. Architecture in plain English

```
projects/<name>/
├── data/
│   ├── eval_set.jsonl          ← your test questions (never changes mid-run)
│   ├── corpus.jsonl            ← PDF chunks for RAG (optional)
│   └── db.json                 ← DB connection config for NLQ (optional)
├── MISSION.json                ← what success looks like (locked before /run)
├── experiment_log.jsonl        ← append-only log of TrialResults
├── budget.jsonl                ← cost ledger (USD per call, per trial)
├── memory/agent_inbox/         ← JSON channel between subagents (Claude Code path)
├── sketch/                     ← E1–E7 compressed view of results (agents query this)
└── results/
    ├── FINAL.md                ← the recommendation (confidence + counterfactual)
    └── winning_variant.json    ← serialized winning config for the chat REPL
```

The optimization loop in pseudocode:
```python
while not terminated:
    arm = bandit.sample_arm(state)           # Thompson sampling
    diff = mutation_library[arm](parent)     # pick a specific mutation
    child = variants.apply_diff(parent, diff)
    if doom_loop.is_duplicate(child): continue
    verdict = skeptic.audit_proposal(...)    # deterministic checks
    if verdict == FAIL: continue
    trial = run.execute_trial(child, eval_set)
    bandit.update_arm(arm, win=trial_beat_prior_best)
    if budget_exhausted or goal_met or iteration_cap: break
```

Everything that writes to disk uses **append-only logs** — no edits, no in-place mutation. `genai replay` re-runs any trial and diffs the metrics.

---

## 8. Extending the framework

### Add a new capability
```python
# lib/capabilities/my_capability.py
from .base import CapabilityBase, CapabilityContext, RunOutput, register_capability
from lib.schemas.variant import Variant
from lib.schemas.eval_case import EvalCase

@register_capability("my_task")
class MyCapability(CapabilityBase):
    primary_metrics = ["judge_score"]
    secondary_metrics = ["p95_latency_ms"]
    allowed_arms = ["prompt_rewrite", "model_swap", "decoding_params"]

    def run_single_case(self, variant: Variant, case: EvalCase, ctx: CapabilityContext) -> RunOutput:
        from lib.registry import model_call
        result = model_call(system=variant.prompt.system, user=case.input, generation=variant.generation)
        return RunOutput(case_id=case.case_id, raw_output=result.text,
                         cost_usd=result.cost_usd, latency_ms=result.latency_ms)
```

Then add `"my_task"` to `MODALITY_OPTIONS` in `web/api.py` and `lib/headless_optimizer.py:_MUTATION_LIBRARY`.

### Add a new metric
```python
# lib/eval.py — add to the _METRIC_REGISTRY dict
"my_metric": _compute_my_metric,   # function(outputs, eval_set, judge_reports) -> MetricSnapshot | None
```

### Swap the LLM vendor
Only change: `lib/registry.py`. Add a branch in `_backend()` and implement `_yourvendor_call()` with the same return type (`ModelCallResult`). Nothing else in the codebase touches vendor-specific code.

---

## 9. Running and reading the tests

```bash
# All tests — no API key required (stub backend)
python -m pytest tests/ -v

# Key test files to read alongside the code:
tests/test_bandit_and_doom_loop.py    ← Thompson sampling persistence + fingerprint detection
tests/test_schemas.py                 ← content-hash stability, mission validation
tests/test_auditor.py                 ← contamination → CATASTROPHIC, cost projection
tests/test_corpus.py                  ← BM25 stemming, chunking, hybrid retrieval
tests/test_db.py                      ← SELECT-only guard, DoS-construct blocks
tests/test_end_to_end.py              ← full pipeline in stub mode (no API key)

# Framework-level sanity check (runs in CI):
python tools/audit_repo.py
```

---

## 10. Things worth trying in your first session

| Try this | What it shows |
|---|---|
| Run `python -m pytest tests/ -v` without any API key | Entire framework is testable in stub mode |
| Open `projects/.templates/_project_template/` | The skeleton every new project copies from |
| Read `seeds/universal_seeds.jsonl` | The 8 universal starting hypotheses the optimizer begins with |
| Read `recipes/rag_qa.json` | How success criteria + judge spec are pre-configured per capability |
| Open Docker UI → New Project → pick RAG QA → upload `tests/fixtures/eval_set_small.jsonl` | End-to-end UI flow in under 5 minutes |
| Set `ANTHROPIC_API_KEY` and run one trial: | |
| `python -c "from lib.registry import model_call; from lib.schemas.variant import GenerationConfig; r = model_call(system='You help.', user='What is 2+2?', generation=GenerationConfig()); print(r.text, r.cost_usd)"` | Confirm real LLM calls flow through the registry correctly |
| Read `lib/headless_optimizer.py:run_optimization()` top to bottom | The cleanest 300-line description of the full loop |
| Check `knowledge/` files after `/contribute` | What the cross-project library accumulates |

---

## 11. Common gotchas

| Symptom | Fix |
|---|---|
| **Finalize shows "no_signal"** | No trials were recorded. The optimizer may have skipped all iterations (Auditor FAIL or eval set < 20 cases). Check the Live activity log for red error lines. |
| **All outputs are `[stub:claude-haiku-4-5:...]`** | No API key set. Add `ANTHROPIC_API_KEY` to `.env` and rebuild Docker, or export it in shell. |
| **`ModuleNotFoundError: lib`** | `pip install -e .` was not run. The package must be installed editable so `lib.*` is importable. |
| **Auditor keeps issuing FAIL on `prior_evidence`** | The proposal's `prior_evidence.reference` is empty. In the headless optimizer this is always set — if you're writing a custom Strategist, ensure the field is populated. |
| **Sentinel calls everything a duplicate** | Doom-loop fingerprints are normalized (lowercased, whitespace-stripped, SHA-256). Two prompts that differ only in casing or spacing collide. This is by design. |
| **Docker 500 on Finalize** | Check Docker terminal for `evalsmith.web — finalize_project raised:` lines. The full traceback is logged there after our latest fix. |
| **Eval set "too small" error** | The Architect requires ≥ 20 cases. Check `len(eval_set.jsonl)` with `wc -l data/eval_set.jsonl`. |

---

## 12. Repo quick-reference

```
lib/                    ← all framework logic (pure Python, no LLM deps required at import time)
├── schemas/            ← pydantic v2 models — the shared language between all agents
├── capabilities/       ← one class per task type; extend CapabilityBase to add yours
├── sketch/             ← E1–E7 layer builders; agents query via MCP, never read raw files
├── registry.py         ← ONLY place that calls an LLM/embedder — swap vendor here
├── run.py              ← Trial executor
├── headless_optimizer.py ← Web UI loop (no Claude Code needed)
└── skeptic.py          ← Auditor checks

web/                    ← FastAPI + Jinja2 + HTMX (Docker surface)
├── api.py              ← 20 routes
├── services.py         ← business logic (thin service layer between routes and lib/)
└── templates/          ← HTML templates (Bootstrap 5 + HTMX, no JS build step)

webui/                  ← Streamlit pages (local dev, excluded from Docker image)
└── pages/              ← 7 screens, maps 1-to-1 with web/templates/

.claude/
├── agents/             ← 10 subagent specs (Architect, Strategist, Scout, Sentinel, Auditor, Mediator, Operator, Inspector, Curator, Provoker)
└── commands/           ← /init /plan /run /resume /status /contribute

mcp_servers/            ← 4 stdio MCP servers (sketch, retrieval, budget, judge)
recipes/                ← pre-configured Mission templates per capability
seeds/                  ← 8 universal starting hypotheses
knowledge/              ← cross-project library (grows with /contribute)
tests/                  ← pytest suite (all pass in stub mode, no API key needed)
tools/                  ← ingest_pdfs.py, introspect_db.py, audit_repo.py, replay_runner.py
docs/                   ← this file + WALKTHROUGH.md, PDF_RAG_GUIDE.md, DATABASES_AND_CHAT.md
```

---

## 13. Questions? Where to dig deeper

| Question | Go read |
|---|---|
| How does the bandit actually pick arms? | `lib/bandit.py` — Thompson sampling, Beta priors, seed-deterministic |
| What exactly does the Auditor check? | `lib/skeptic.py` — prior evidence, cost projection, eval contamination, judge calibration |
| How are metrics computed + CIs derived? | `lib/eval.py` — bootstrap CI over eval cases |
| How does cross-project knowledge accumulate? | `tools/post_merge_extractor.py` + `lib/retrieval.py` |
| What does the LLM judge actually do? | `lib/judges.py` + `recipes/<capability>.json` default_judge section |
| How does the state machine handle breakthrough? | `lib/state.py:_termination_check()` + `lib/state.py:_breakthrough_check()` |
| How do I reproduce any trial exactly? | `genai replay <project>` → calls `tools/replay_runner.py` |
