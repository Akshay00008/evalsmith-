# tools/audit_repo.py
# Sanity audit for the framework repo itself. Verifies:
#   * Every capability declares only metrics that exist in lib/eval.py.
#   * Every subagent .md has the required frontmatter fields.
#   * Every recipe parses as a valid Mission shape (modulo eval_set_hash).
#   * The knowledge/ files are jsonl-valid.
#
# Intended for CI on the framework repo, not on individual projects.

from __future__ import annotations
from pathlib import Path
import json
import sys


def audit() -> int:
    root = Path(__file__).resolve().parent.parent
    errors: list[str] = []

    # 1. Capabilities <-> metric registry.
    sys.path.insert(0, str(root))
    from lib import eval as eval_mod
    from lib import capabilities as caps_mod
    from lib.capabilities.registry import _REGISTRY  # type: ignore

    declared_metrics: set[str] = set()
    for name, cls in _REGISTRY.items():
        declared_metrics.update(cls.primary_metrics)
        declared_metrics.update(cls.secondary_metrics)
    missing = declared_metrics - set(eval_mod._METRICS.keys())  # type: ignore
    if missing:
        errors.append(f"Capabilities declare metrics with no implementation: {sorted(missing)}")

    # 2. Subagent frontmatter.
    for md in (root / ".claude" / "agents").glob("*.md"):
        text = md.read_text(encoding="utf-8")
        if not text.startswith("---"):
            errors.append(f"{md.name}: missing frontmatter")
            continue
        if "name:" not in text or "description:" not in text:
            errors.append(f"{md.name}: missing name/description in frontmatter")

    # 3. Recipes parse.
    for r in (root / "recipes").glob("*.json"):
        try:
            obj = json.loads(r.read_text(encoding="utf-8"))
            for k in ("composition", "success_criteria"):
                if k not in obj:
                    errors.append(f"recipe {r.name}: missing {k}")
        except json.JSONDecodeError as e:
            errors.append(f"recipe {r.name}: invalid JSON ({e})")

    # 4. Knowledge jsonl files parse.
    for f in (root / "knowledge").glob("*.jsonl"):
        with f.open("r", encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    json.loads(line)
                except json.JSONDecodeError as e:
                    errors.append(f"knowledge/{f.name}:{i}: invalid JSON ({e})")
                    break

    if errors:
        print("AUDIT FAILED")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("AUDIT OK")
    return 0


if __name__ == "__main__":
    sys.exit(audit())
