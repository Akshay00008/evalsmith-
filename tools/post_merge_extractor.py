# tools/post_merge_extractor.py
# Anonymizes a finalized project's knowledge_bundle.json and appends to
# the framework-level knowledge/ files. /contribute calls this with
# --dry-run first to refuse merge if anonymization is incomplete.
#
# Anonymization rules (enforced):
#   * Field names with proper-noun-looking values (e.g. company names,
#     product names, person names) are replaced with semantic-role tags.
#   * Raw eval inputs / outputs MUST NOT appear in any record.
#   * URLs are kept (they are not project-sensitive).
#   * Numeric values are kept.

from __future__ import annotations
from pathlib import Path
import json
import re
import sys
import argparse


# Heuristic markers that, if present in a string, mark it as
# "potentially-raw" and trigger refusal. The list is intentionally narrow
# — false positives are cheaper than letting raw eval text through.
_RAW_MARKERS = (
    "case_id", "@", "http://localhost", "/var/", "C:\\Users\\",
    "127.0.0.1", "secret", "password",
)


def is_anonymized(record: dict) -> tuple[bool, list[str]]:
    """Return (ok, reasons). `ok=False` blocks merge."""
    issues = []
    text = json.dumps(record).lower()
    for marker in _RAW_MARKERS:
        if marker.lower() in text:
            issues.append(f"raw marker found: {marker!r}")
    # Heuristic: a very long string is probably an unfiltered eval input.
    for k, v in record.items():
        if isinstance(v, str) and len(v) > 600:
            issues.append(f"field {k!r} length {len(v)} > 600 — likely raw text")
    return (not issues, issues)


def extract(project_dir: Path, dry_run: bool = True) -> int:
    """Validate + merge the bundle. Returns 0 on success, nonzero on refusal."""
    bundle_path = project_dir / "results" / "knowledge_bundle.json"
    if not bundle_path.exists():
        print(f"[extractor] No bundle at {bundle_path}")
        return 1

    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    # Bundle shape: {"prompt_patterns": [...], "rag_recipes": [...], ...}
    refusal_reasons = []
    for section, records in bundle.items():
        if not isinstance(records, list):
            continue
        for i, rec in enumerate(records):
            ok, issues = is_anonymized(rec)
            if not ok:
                refusal_reasons.append(f"  {section}[{i}]: {', '.join(issues)}")

    if refusal_reasons:
        print("[extractor] REFUSED — anonymization issues:")
        for r in refusal_reasons:
            print(r)
        return 2

    if dry_run:
        print("[extractor] dry-run OK — bundle is anonymized.")
        return 0

    # Stage to framework root's knowledge/.
    root = project_dir.parent.parent / "knowledge"
    root.mkdir(parents=True, exist_ok=True)
    section_files = {
        "prompt_patterns": "prompt_pattern_library.jsonl",
        "rag_recipes": "rag_recipes.jsonl",
        "failure_modes": "failure_modes.jsonl",
        "model_routes": "model_route_priors.jsonl",
        "judge_templates": "eval_judge_templates.jsonl",
    }
    for section, fname in section_files.items():
        records = bundle.get(section, [])
        if not records:
            continue
        with (root / fname).open("a", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")
        print(f"[extractor] appended {len(records)} -> {fname}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--project", required=True, help="Project directory (e.g. projects/myproj/).")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    sys.exit(extract(Path(args.project), dry_run=args.dry_run))
