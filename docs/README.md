# evalsmith — Documentation index

Quick reference for finding the right guide.

## I want to...

| Goal                                                              | Read                                                     |
|-------------------------------------------------------------------|----------------------------------------------------------|
| Use evalsmith without writing code (web UI, PM/BA-friendly)       | [NONTECH_GUIDE.md](NONTECH_GUIDE.md)                     |
| Understand the architecture and concepts                          | [../README.md](../README.md)                             |
| Install + run a project end-to-end via CLI + Claude Code          | [WALKTHROUGH.md](WALKTHROUGH.md)                         |
| Use my own PDFs as a RAG corpus                                   | [PDF_RAG_GUIDE.md](PDF_RAG_GUIDE.md)                     |
| Connect a SQL / Oracle / etc. database for NLQ                    | [DATABASES_AND_CHAT.md](DATABASES_AND_CHAT.md) (Part 1)  |
| Test the winning variant in an interactive chat                   | [DATABASES_AND_CHAT.md](DATABASES_AND_CHAT.md) (Part 2)  |

## Reading order for new users

1. **[../README.md](../README.md)** — what evalsmith is, the 7 flow diagrams, capability table, philosophy. ~15 min skim.
2. **[WALKTHROUGH.md](WALKTHROUGH.md)** — every command from install to FINAL.md, with a 20-case worked example. ~30 min hands-on.
3. **One of:**
   - [PDF_RAG_GUIDE.md](PDF_RAG_GUIDE.md) if your data is documents.
   - [DATABASES_AND_CHAT.md](DATABASES_AND_CHAT.md) if your data is in a DB.
4. Run `genai chat <project>` after `/run` terminates — covered in [DATABASES_AND_CHAT.md § Part 2](DATABASES_AND_CHAT.md#part-2--chat-repl).

## Quick command reference

```bash
# Project lifecycle
genai new-project <name> [--recipe <r>]             # scaffold
genai list                                          # all projects
genai status <name>                                 # one-screen snapshot

# Data ingestion (per project, when applicable)
python tools/ingest_pdfs.py --project projects/<name>     # PDFs  -> data/corpus.jsonl
python tools/introspect_db.py --project projects/<name>   # DB schema -> data/schema.txt

# In Claude Code (slash commands)
/init <name>       # profile eval set, baseline trial
/plan <name>       # Architect Q&A, lock MISSION.json
/run <name>        # autonomous optimization loop
/status <name>     # read-only status (safe during /run)
/resume <name>     # continue interrupted /run
/contribute <name> # stage knowledge bundle for merge

# Post-/run
genai chat <name>                                    # interactive REPL on winning variant
genai library <section> [--tag <t>]                  # inspect knowledge/
genai replay <name>                                  # verify determinism
```

## Modality cheat sheet

| Your situation                                  | Recipe                  | Capability         | Extra setup                                                |
|-------------------------------------------------|-------------------------|--------------------|------------------------------------------------------------|
| QA over a corpus (PDFs, docs, knowledge base)   | `rag_qa`                | `rag_qa`           | `tools/ingest_pdfs.py` → `data/corpus.jsonl`               |
| NL question → SQL on your DB                    | `nlq_sql`               | `nlq_to_query`     | `data/db.json` + `tools/introspect_db.py`                  |
| Open-ended research with citations              | `research_citation`     | `research_agent`   | (web access via tool-use)                                  |
| Extract structured data from documents          | `insight_extraction`    | `insight_agent`    | Each doc → one EvalCase.input                              |
| Ranked search results with LLM rerank           | `search_engine`         | `search_engine`    | `data/corpus.jsonl` + EvalCase.expected = ranked doc_id list |
| Multi-turn conversational assistant             | `chatbot_support`       | `chatbot`          | EvalCase.input = turn list                                 |

## Test commands

```bash
python -m pytest tests/ -v        # 42 tests, ~1s with stub backend
python tools/audit_repo.py        # schema + frontmatter + JSONL sanity
```

## Found something missing or unclear?

Open an issue at https://github.com/AkshayDat/evalsmith- with the doc filename + section.
