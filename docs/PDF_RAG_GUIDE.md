# Running evalsmith on your own PDFs

How to point evalsmith at a folder of PDFs and have it optimize a RAG pipeline over them.

> **Related guides**
> - [WALKTHROUGH.md](WALKTHROUGH.md) — the general pipeline tour. Read first if you're new.
> - [DATABASES_AND_CHAT.md](DATABASES_AND_CHAT.md) — if your input is a database instead of PDFs (NLQ missions), or you want the `genai chat` REPL deep-dive.

PDFs play one of two roles in a project:

| Role         | What it means                                                  | Capability                  |
|--------------|----------------------------------------------------------------|-----------------------------|
| **Corpus**   | PDFs are what the system *retrieves from* to answer questions. | `rag_qa`, `search_engine`   |
| **Input**    | Each PDF is the *input* of one eval case (e.g. extract clauses from this contract). | `insight_agent` |

This guide covers the **corpus** path — the common case. The insight path is similar but each PDF goes into `EvalCase.input` instead of a corpus file (see the [end of this doc](#alternative-pdfs-as-eval-inputs-not-corpus)).

---

## Table of Contents

1. [The mental model](#1-the-mental-model)
2. [End-to-end flow](#2-end-to-end-flow)
3. [Step 1 — scaffold a project](#3-step-1--scaffold-a-project)
4. [Step 2 — drop PDFs into raw_pdfs/](#4-step-2--drop-pdfs-into-raw_pdfs)
5. [Step 3 — ingest PDFs into corpus.jsonl](#5-step-3--ingest-pdfs-into-corpusjsonl)
6. [Step 4 — write the eval set](#6-step-4--write-the-eval-set)
7. [Step 5 — `/init`, `/plan`, `/run`](#7-step-5--init-plan-run)
8. [What the retriever does under the hood](#8-what-the-retriever-does-under-the-hood)
9. [Tuning the ingestion](#9-tuning-the-ingestion)
10. [Common gotchas](#10-common-gotchas)
11. [Alternative: PDFs as eval inputs (not corpus)](#alternative-pdfs-as-eval-inputs-not-corpus)

---

## 1. The mental model

evalsmith doesn't read PDFs directly. It works against a flat **corpus file** (`<project>/data/corpus.jsonl`) where each row is one **chunk** of text with a stable `doc_id`. Your job is:

1. Convert PDFs → chunks (done by `tools/ingest_pdfs.py`).
2. Write an eval set of questions, each citing the `doc_id` of the chunk(s) that contain the answer.

Once those two files exist, the rest is identical to any other RAG project: `/init` → `/plan` → `/run` → `FINAL.md`.

```
PDFs in raw_pdfs/      tools/ingest_pdfs.py        corpus.jsonl       lib/corpus.py BM25
   manual_v1.pdf  ────────────────────────────►   doc_id, text  ──────────────────►  retriever returns
   policy.pdf                                     per chunk           top-k chunks
                                                                       ▲
                                                                       │ at /run time
                          eval_set.jsonl                              every iter
                          question + relevant_doc_ids
```

The `relevant_doc_ids` field in each eval case is what drives the **recall@k** metric — it tells the framework which chunks *should* have been retrieved for that question.

---

## 2. End-to-end flow

```
1. genai new-project my_pdfs --recipe rag_qa
2. mkdir projects/my_pdfs/data/raw_pdfs
3. cp /path/to/your/*.pdf projects/my_pdfs/data/raw_pdfs/
4. pip install pypdf                                # one-time
5. python tools/ingest_pdfs.py --project projects/my_pdfs
6. # write projects/my_pdfs/data/eval_set.jsonl     (questions + relevant_doc_ids)
7. # In Claude Code:
   /init my_pdfs
   /plan my_pdfs
   /run my_pdfs
8. # Read projects/my_pdfs/results/FINAL.md
9. genai chat my_pdfs                               # talk to the winning variant
```

Sections 3–7 walk each step in detail. Section 7 (Step 6) covers the chat REPL.

---

## 3. Step 1 — scaffold a project

```bash
genai new-project my_pdfs --recipe rag_qa
```

This creates `projects/my_pdfs/` with the standard skeleton plus a copy of the `rag_qa` recipe (so `/plan` gets sensible defaults: judge_score >= 0.80 target, 0.65 floor, `search_qa` domain).

---

## 4. Step 2 — drop PDFs into `raw_pdfs/`

```bash
mkdir -p projects/my_pdfs/data/raw_pdfs
cp ~/Documents/*.pdf projects/my_pdfs/data/raw_pdfs/
```

There's no naming convention — files can be `manual_v1.pdf`, `Q3_policy.pdf`, anything. The chunk `doc_id`s will encode the filename + page range + offset so they survive a re-ingestion.

> **Mixed content?** If your PDFs are mostly scanned images (no extractable text), you'll need OCR first — pypdf can't extract from rasterized pages. Run them through `tesseract` or `ocrmypdf` before dropping into `raw_pdfs/`.

---

## 5. Step 3 — ingest PDFs into `corpus.jsonl`

One-time install (lightweight, pure-Python):

```bash
pip install pypdf
```

Then ingest:

```bash
python tools/ingest_pdfs.py --project projects/my_pdfs
```

Expected output:

```
Found 3 PDF(s) in projects/my_pdfs/data/raw_pdfs:
  - manual_v1.pdf
  - policy.pdf
  - faq.pdf

Processing manual_v1.pdf ...
  42 pages, 87,431 chars extracted
  -> 64 chunks
Processing policy.pdf ...
  8 pages, 12,108 chars extracted
  -> 9 chunks
Processing faq.pdf ...
  5 pages, 6,433 chars extracted
  -> 5 chunks

Total: 78 chunks across 3 PDF(s)

Wrote projects/my_pdfs/data/corpus.jsonl
```

Verify a sample line:

```bash
head -1 projects/my_pdfs/data/corpus.jsonl
```

```json
{"page_start": 1, "page_end": 1, "char_start": 0, "char_end": 1392, "text": "Refunds are available within 30 days of purchase ...", "source_pdf": "manual_v1.pdf", "doc_id": "a4f8e92b1c5d7e90"}
```

**Note that `doc_id` is deterministic** — same `(filename, page range, offset)` always produces the same id. Re-running ingestion later (e.g. with different chunk sizes) re-derives ids stably, so eval cases referencing them don't break unless the chunking changes.

### Useful flags

| Flag             | Default | When to change                                                                |
|------------------|---------|-------------------------------------------------------------------------------|
| `--chunk-size`   | 1500    | Smaller (e.g. 800) for short fact-style answers; larger (3000+) for legal/technical context that needs continuity. |
| `--overlap`      | 200     | Bump to 400+ if your content has tight cross-references between adjacent passages. |
| `--dry-run`      | off     | Print summary + one sample chunk without writing anything. Sanity-check before committing.|

### Preview chunks before writing

```bash
python tools/ingest_pdfs.py --project projects/my_pdfs --dry-run
```

---

## 6. Step 4 — write the eval set

This is the work that **actually determines optimization quality**. The framework can only optimize what the eval set measures.

Each row of `projects/my_pdfs/data/eval_set.jsonl` is one question:

```jsonl
{"case_id": "q001", "input": "What is the refund window?", "expected": "30 days from purchase.", "tags": ["refund","short"], "relevant_doc_ids": ["a4f8e92b1c5d7e90"]}
{"case_id": "q002", "input": "Can I refund a digital product?", "expected": "Digital products non-refundable except within 24h of accidental purchase.", "tags": ["refund","digital"], "relevant_doc_ids": ["b3c9f1d27e604a18"]}
```

### How to find the `doc_id` for a chunk

When writing eval cases, you need to know which chunk contains the answer for each question. Two ways:

**Option A: grep the corpus**

```bash
grep -m1 "refund" projects/my_pdfs/data/corpus.jsonl | python -c "import sys,json; r=json.loads(sys.stdin.read()); print('doc_id:', r['doc_id'], 'page:', r['page_start'])"
```

**Option B: interactive — load corpus, print chunks containing keywords**

```python
# scratch.py
from pathlib import Path
from lib.corpus import load_chunks
chunks = load_chunks(Path("projects/my_pdfs"))
for c in chunks:
    if "refund" in c["text"].lower():
        print(c["doc_id"], c["page_start"], c["text"][:120])
```

> **At minimum 20 cases.** The Architect rejects smaller eval sets. Real RAG projects benefit from 100–500 — the bootstrap CIs only become useful around n=30.

### What `expected` should contain

| Eval strategy                                  | What `expected` should be                                          |
|-----------------------------------------------|--------------------------------------------------------------------|
| `exact_match`                                  | Literal target string. Whitespace + case normalized at compare.    |
| `judge_llm` (default for rag_qa)               | The reference answer in natural language. Judge scores closeness.  |
| `null` + tag `should_refuse`                   | Eval case tests refusal calibration. Provoker also uses these.     |

---

## 7. Step 5 — `/init`, `/plan`, `/run`

Identical to the standard walkthrough. In Claude Code (or via the CLI for path B):

```
/init my_pdfs        # profiles eval set; baseline trial; builds E1 sketch layer
/plan my_pdfs        # Architect Q&A → locked MISSION.json
/run my_pdfs         # autonomous loop; Strategist tunes prompt / retriever / chunking / model
```

During `/run`, the Strategist proposes things like:

```
[iter  3] arm=retriever_change     → judge_score 0.61 ± 0.05 (Δ +0.13)  (top_k 5 -> 10)
[iter  7] arm=chunking_change      → judge_score 0.68 ± 0.04 (Δ +0.07)  (chunk_size 1500 -> 800)
[iter 11] arm=rerank_add_or_change → judge_score 0.74 ± 0.04 (Δ +0.06)  (added bge-reranker-v2-m3)
[iter 14] arm=prompt_rewrite       → judge_score 0.81 ± 0.03 (Δ +0.07)  (added explicit citation requirement)
```

When it terminates, `projects/my_pdfs/results/FINAL.md` will name the winning configuration. The recommended variant's `retrieval` block is the RAG config you ship to production.

### Step 6 — Chat with the winning variant

Before shipping, talk to it:

```bash
genai chat my_pdfs
```

For RAG missions, each turn retrieves top-k chunks from `data/corpus.jsonl`, builds the prompt, and generates an answer — exactly what the deployed system would do. Citations are shown:

```
you> Can I refund a digital product purchased last week?

bot> Digital products are non-refundable except within 24 hours of accidental
     purchase. [b3c9f1d27e604a18]
   (retrieved: b3c9f1d2, a4f8e92b, c5e1a04f · 487+34 tok · $0.0008)
```

This is also how you grow the eval set: any chat answer that looks wrong is a question + correct answer pair waiting to be added to `data/eval_set.jsonl`. See [DATABASES_AND_CHAT.md § 2.7](DATABASES_AND_CHAT.md#27-using-chat-to-grow-your-eval-set) for the workflow loop.

---

## 8. What the retriever does under the hood

`lib/corpus.py` ships three retrievers; choose one in your Mission's `retrieval.retriever_kind`:

| `retriever_kind` | Backed by                        | Pros                              | Cons                                          |
|------------------|----------------------------------|-----------------------------------|-----------------------------------------------|
| `bm25`           | Built-in pure-Python BM25        | No model download; fast; lexical  | Misses paraphrases & synonyms                 |
| `dense`          | sentence-transformers + cosine   | Captures semantics                | Needs `pip install sentence-transformers`; warmup cost |
| `hybrid`         | RRF(BM25, dense)                 | Robust default; best of both      | Slowest                                       |

Whichever you pick, the framework reads from the *same* `corpus.jsonl` — no re-ingestion.

The Strategist will try multiple retrievers across iterations (`retriever_change` is a bandit arm), so even if you start with BM25 it may end up recommending `hybrid`.

---

## 9. Tuning the ingestion

Three knobs at ingestion time. The Strategist can tune retrieval-side parameters (top_k, reranker) at run time, but **chunking is baked into the corpus** — you control it via `tools/ingest_pdfs.py`.

| Knob              | Effect                                                                              |
|-------------------|-------------------------------------------------------------------------------------|
| `--chunk-size`    | Smaller chunks → higher precision but answers may straddle chunk boundaries.       |
| `--overlap`       | Larger overlap → safer boundaries, larger corpus.                                  |
| Paragraph awareness | The ingest script splits on `\n\n` when possible. Long unbroken paragraphs fall back to fixed-window. |

Re-ingest and re-`/init` if you change chunk size — the `doc_id`s change, so old eval cases' `relevant_doc_ids` may no longer resolve.

> **Iterate the eval set too.** Use the Inspector's `synthesis_NNNN.md` notes and `failure_clusters` to discover what questions you should be testing for but aren't.

---

## 10. Common gotchas

### `ERROR: pypdf is not installed`
Run `pip install pypdf`. We keep it as a lazy import so the framework doesn't force-install it.

### Extracted text is garbage (lots of "??" or empty pages)
The PDF is image-based (scanned). Run it through OCR first:
```bash
ocrmypdf input.pdf output.pdf
```
Then put `output.pdf` in `raw_pdfs/`.

### Eval cases reference doc_ids that don't exist in the corpus
- You re-ingested with a different `--chunk-size`; ids changed. Regenerate eval set or pin chunking.
- Typo in the eval set's `relevant_doc_ids`.

Check: every relevant_doc_id should appear as a `doc_id` in corpus.jsonl.

```bash
python -c "
from pathlib import Path
from lib.corpus import load_chunks
import json
chunks = {c['doc_id'] for c in load_chunks(Path('projects/my_pdfs'))}
with open('projects/my_pdfs/data/eval_set.jsonl') as f:
    for line in f:
        case = json.loads(line)
        for d in case.get('relevant_doc_ids') or []:
            if d not in chunks:
                print('MISSING:', case['case_id'], '->', d)
"
```

### `recall_at_5` is consistently 0
- Your `relevant_doc_ids` are wrong (see above).
- BM25 has no shared terms between query and chunk (e.g. query says "refund" but chunk says "money back"). Try `retriever_kind=dense` or `hybrid`.

### Corpus is huge (~100k chunks); dense retrieval is slow
Built-in dense retriever embeds the whole corpus on first call (cached in process). Above ~10k chunks consider a real vector DB:
- Install `chromadb`: `pip install chromadb`
- Replace `lib/corpus.dense_retrieve` with a chroma-backed implementation (the interface is small — see `bm25_retrieve` for the contract).

### Multilingual / non-Latin scripts
The built-in tokenizer (`_WORD_RE` in `lib/corpus.py`) is ASCII-biased. For CJK / Devanagari corpora, replace it with a real tokenizer (e.g. `tantivy`, `jieba`, or `indic-tokenizer`).

---

## Alternative: PDFs as eval inputs (not corpus)

Some tasks treat each PDF as the *input* of one eval case rather than a retrieval target. Examples:

- Contract analysis: "extract parties + effective date from this PDF"
- Report summarization: "summarize this 30-page report"
- Compliance review: "list every clause that violates GDPR"

For these, use the **`insight_agent`** capability (recipe `insight_extraction.json`) and put the extracted PDF text directly in `EvalCase.input`:

```jsonl
{"case_id": "contract_001", "input": "<full extracted text of contract_001.pdf>", "expected": {"parties": ["Acme", "Globex"], "effective_date": "2024-01-15"}, "tags": ["nda","short"]}
```

A small one-off script can convert PDFs → eval cases:

```python
# pdfs_to_eval_set.py
from pathlib import Path
from pypdf import PdfReader
import json

raw_dir = Path("projects/my_extraction/data/raw_pdfs")
out = Path("projects/my_extraction/data/eval_set.jsonl")

with out.open("w", encoding="utf-8") as f:
    for pdf in sorted(raw_dir.glob("*.pdf")):
        text = "\n\n".join((p.extract_text() or "") for p in PdfReader(str(pdf)).pages)
        # You'll need to provide the gold expected answer per PDF manually.
        case = {
            "case_id": pdf.stem,
            "input": text,
            "expected": None,        # fill in gold answers
            "tags": ["contract"],
        }
        f.write(json.dumps(case) + "\n")
```

Then `/init`, `/plan` (pick `insight_extraction` recipe), `/run`. The Strategist will tune the *extraction* prompt + schema rather than retrieval.

---

For deeper architecture detail see [../README.md](../README.md). For general project setup see [WALKTHROUGH.md](WALKTHROUGH.md).
