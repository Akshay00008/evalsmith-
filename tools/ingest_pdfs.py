# tools/ingest_pdfs.py
# Ingest PDFs from <project>/data/raw_pdfs/ into <project>/data/corpus.jsonl.
#
# Each row of corpus.jsonl is one *chunk*:
#   {"doc_id": "<unique chunk id>", "source_pdf": "manual.pdf",
#    "page_start": int, "page_end": int,
#    "char_start": int, "char_end": int, "text": "..."}
#
# doc_ids are the stable handles EvalCase.relevant_doc_ids references for
# recall@k scoring. They are derived from (filename, page range, position)
# so re-running ingestion produces stable ids — replays still work.
#
# Dependencies: pypdf (lazy-imported). Install once:
#     pip install pypdf

from __future__ import annotations
from pathlib import Path
from typing import Iterable, Optional
import argparse
import hashlib
import json
import sys


def _extract_pdf_text(pdf_path: Path) -> list[tuple[int, str]]:
    """Return list of (page_number, page_text) tuples. Page numbers are
    1-indexed to match what users see in a PDF reader.

    Lazy-imports pypdf so this script is only as expensive to load as it
    needs to be — useful when running --help or --dry-run."""
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        print(
            "ERROR: pypdf is not installed. Install with:\n"
            "    pip install pypdf\n",
            file=sys.stderr,
        )
        sys.exit(2)

    reader = PdfReader(str(pdf_path))
    out = []
    for i, page in enumerate(reader.pages, start=1):
        # extract_text returns None on rare unparseable pages — coerce to "".
        text = page.extract_text() or ""
        # Normalize whitespace: PDFs frequently emit doubled spaces and
        # awkward line breaks. We collapse runs of whitespace into single
        # spaces, but keep paragraph breaks (double newlines) intact.
        text = _normalize_whitespace(text)
        out.append((i, text))
    return out


def _normalize_whitespace(text: str) -> str:
    """Collapse pathological PDF whitespace without destroying paragraph
    structure. Keeps double-newlines (paragraph breaks); collapses
    single-newlines and runs of spaces."""
    import re
    # Preserve paragraph breaks as a placeholder, then collapse the rest,
    # then restore. Avoids the regex-soup that handling them inline causes.
    placeholder = "PARAGRAPH"
    text = re.sub(r"\n\s*\n", placeholder, text)
    text = re.sub(r"\s+", " ", text)
    text = text.replace(placeholder, "\n\n")
    return text.strip()


def _doc_id(source_pdf: str, page_start: int, page_end: int, char_start: int) -> str:
    """Deterministic chunk id. Same (file, page, offset) -> same id, so
    re-ingestion does not break eval cases that reference these ids."""
    payload = f"{source_pdf}|p{page_start}-{page_end}|c{char_start}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _chunk_pages(
    pages: list[tuple[int, str]],
    *,
    chunk_size_chars: int,
    overlap_chars: int,
) -> Iterable[dict]:
    """Yield chunk dicts. Strategy:
      - Concatenate pages into one stream with a marker so we know which
        page a given char offset belongs to.
      - Slide a window of `chunk_size_chars` over the stream with
        `overlap_chars` overlap.
    This gives semantically continuous chunks even when content spans
    pages (common in tables, footnotes, and multi-page bullet lists).
    """
    # Use lib.corpus.chunk_text to do the actual splitting; it knows about
    # paragraph-aware vs fixed-window fallback.
    from lib.corpus import chunk_text

    # Reconstruct full text + a page-offset map so we can compute
    # page_start / page_end per chunk.
    full_text = ""
    page_breaks: list[tuple[int, int]] = []  # (page_num, char_offset_at_start_of_page)
    for page_num, page_text in pages:
        page_breaks.append((page_num, len(full_text)))
        if full_text:
            full_text += "\n\n"
        full_text += page_text

    for char_start, char_end, chunk in chunk_text(
        full_text,
        chunk_size_chars=chunk_size_chars,
        overlap_chars=overlap_chars,
    ):
        # Determine which page range this chunk spans.
        page_start = page_end = page_breaks[0][0] if page_breaks else 1
        for page_num, offset in page_breaks:
            if offset <= char_start:
                page_start = page_num
            if offset <= char_end:
                page_end = page_num
            else:
                break
        yield {
            "page_start": page_start,
            "page_end": page_end,
            "char_start": char_start,
            "char_end": char_end,
            "text": chunk.strip(),
        }


def ingest_project(
    project_dir: Path,
    *,
    chunk_size_chars: int = 1500,
    overlap_chars: int = 200,
    dry_run: bool = False,
) -> int:
    """Walk <project>/data/raw_pdfs/, ingest each PDF, write corpus.jsonl.
    Returns the number of chunks emitted."""
    raw_dir = project_dir / "data" / "raw_pdfs"
    out_path = project_dir / "data" / "corpus.jsonl"

    if not raw_dir.exists():
        print(f"ERROR: {raw_dir} does not exist.")
        print(f"       Create it and drop PDFs there, then re-run.")
        sys.exit(1)

    pdfs = sorted(raw_dir.glob("*.pdf"))
    if not pdfs:
        print(f"ERROR: no PDFs found under {raw_dir}")
        sys.exit(1)

    print(f"Found {len(pdfs)} PDF(s) in {raw_dir}:")
    for p in pdfs:
        print(f"  - {p.name}")
    print()

    all_chunks: list[dict] = []
    for pdf in pdfs:
        print(f"Processing {pdf.name} ...")
        pages = _extract_pdf_text(pdf)
        n_chars = sum(len(t) for _, t in pages)
        print(f"  {len(pages)} pages, {n_chars} chars extracted")

        n_before = len(all_chunks)
        for chunk in _chunk_pages(pages, chunk_size_chars=chunk_size_chars, overlap_chars=overlap_chars):
            chunk["source_pdf"] = pdf.name
            chunk["doc_id"] = _doc_id(pdf.name, chunk["page_start"], chunk["page_end"], chunk["char_start"])
            all_chunks.append(chunk)
        print(f"  -> {len(all_chunks) - n_before} chunks")

    print(f"\nTotal: {len(all_chunks)} chunks across {len(pdfs)} PDF(s)")

    if dry_run:
        print(f"(dry-run; not writing to {out_path})")
        # Show a sample so the user can sanity-check the chunking before
        # committing the full corpus to disk.
        if all_chunks:
            sample = all_chunks[0]
            print("\nSample chunk:")
            print(f"  doc_id: {sample['doc_id']}")
            print(f"  source_pdf: {sample['source_pdf']}")
            print(f"  pages: {sample['page_start']}-{sample['page_end']}")
            print(f"  text (first 200 chars): {sample['text'][:200]!r}")
        return len(all_chunks)

    # Write the corpus file. We over-write rather than append so
    # re-ingestion produces a clean, deduplicated corpus.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for chunk in all_chunks:
            f.write(json.dumps(chunk, ensure_ascii=False) + "\n")
    print(f"\nWrote {out_path}")
    return len(all_chunks)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest PDFs from <project>/data/raw_pdfs/ into a corpus.jsonl that the framework's retriever can query.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--project", required=True, help="Project directory (e.g. projects/my_rag/).")
    parser.add_argument("--chunk-size", type=int, default=1500, help="Chunk size in characters (~400 tokens at chars/4).")
    parser.add_argument("--overlap", type=int, default=200, help="Overlap between consecutive chunks in characters.")
    parser.add_argument("--dry-run", action="store_true", help="Print summary + a sample chunk without writing corpus.jsonl.")
    args = parser.parse_args()

    project_dir = Path(args.project).resolve()
    if not project_dir.exists():
        print(f"ERROR: project dir not found: {project_dir}")
        sys.exit(1)

    ingest_project(
        project_dir,
        chunk_size_chars=args.chunk_size,
        overlap_chars=args.overlap,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    # Make sure the framework's lib/ is importable when this is run as a
    # script (python tools/ingest_pdfs.py ...).
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    main()
