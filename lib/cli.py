# lib/cli.py
# The `genai` CLI — the non-Claude-Code surface. Exposes the verbs that
# don't require Claude Code's Task/MCP integration: project creation,
# listing, status, library inspection, and replay.

from __future__ import annotations
from pathlib import Path
from typing import Optional
import json
import shutil
import time

import typer
from rich.console import Console
from rich.table import Table

from . import __version__ as FRAMEWORK_VERSION

app = typer.Typer(help="AgenticGenAIDevTool CLI — non-Claude-Code surface.")
console = Console()


# ---------------------------------------------------------------------------
# Path helpers — the framework root is wherever this file lives.
# ---------------------------------------------------------------------------

def _framework_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _projects_dir() -> Path:
    return _framework_root() / "projects"


# ---------------------------------------------------------------------------
# new-project
# ---------------------------------------------------------------------------

@app.command("new-project")
def new_project(
    name: str = typer.Argument(..., help="Project name. Must be filesystem-safe."),
    recipe: Optional[str] = typer.Option(None, "--recipe", help="Recipe to copy defaults from (e.g. rag_qa, chatbot_support)."),
    fork: Optional[str] = typer.Option(None, "--fork", help="Existing project to fork — copies MISSION+sketch but starts a fresh experiment log."),
):
    """Create a new project workspace under projects/<name>/."""
    target = _projects_dir() / name
    if target.exists():
        console.print(f"[red]Project {name!r} already exists at {target}.[/red]")
        raise typer.Exit(1)

    template = _projects_dir() / ".templates" / "_project_template"
    if not template.exists():
        console.print(f"[red]Project template missing at {template}.[/red]")
        raise typer.Exit(1)

    # Copy the skeleton, then post-process. shutil.copytree handles the whole
    # tree including the .gitignore and example eval set.
    shutil.copytree(template, target)

    # Stamp the project metadata.
    proj_meta = json.loads((target / "PROJECT.json").read_text(encoding="utf-8"))
    proj_meta["name"] = name
    proj_meta["framework_version"] = FRAMEWORK_VERSION
    proj_meta["created_at_unix"] = time.time()
    (target / "PROJECT.json").write_text(json.dumps(proj_meta, indent=2), encoding="utf-8")

    # Forking: copy MISSION + sketch from source, but never the experiment log.
    if fork:
        src = _projects_dir() / fork
        if not src.exists():
            console.print(f"[yellow]Warning: source project {fork!r} not found; created without fork.[/yellow]")
        else:
            for f in ("MISSION.json",):
                if (src / f).exists():
                    shutil.copy(src / f, target / f)
            if (src / "sketch").exists():
                shutil.copytree(src / "sketch", target / "sketch", dirs_exist_ok=True)
            console.print(f"[green]Forked from {fork!r}.[/green]")

    # If a recipe was named, drop a copy alongside MISSION as recipe.json so
    # /plan can read its defaults without re-resolving the path.
    if recipe:
        rp = _framework_root() / "recipes" / f"{recipe}.json"
        if rp.exists():
            shutil.copy(rp, target / "recipe.json")
            console.print(f"[green]Recipe {recipe!r} staged at recipe.json.[/green]")
        else:
            console.print(f"[yellow]Recipe {recipe!r} not found; project created without one.[/yellow]")

    console.print(f"[bold green]Created project[/bold green] [cyan]{name}[/cyan] at [dim]{target}[/dim]")
    console.print("Next: drop your eval set into data/eval_set.jsonl, then run /init in Claude Code.")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

@app.command("list")
def list_projects():
    """List all projects with their current status."""
    pdir = _projects_dir()
    if not pdir.exists():
        console.print("No projects directory.")
        return
    table = Table(title="Projects")
    table.add_column("Name", style="cyan")
    table.add_column("Status")
    table.add_column("Iter")
    table.add_column("Best metric")
    table.add_column("Budget $")
    for sub in sorted(pdir.iterdir()):
        if not sub.is_dir() or sub.name.startswith("."):
            continue
        proj = json.loads((sub / "PROJECT.json").read_text(encoding="utf-8")) if (sub / "PROJECT.json").exists() else {}
        run_state = sub / "RUN_STATE.json"
        iter_str = "-"
        best_str = "-"
        if run_state.exists():
            rs = json.loads(run_state.read_text(encoding="utf-8"))
            iter_str = str(rs.get("current_iteration", 0))
        budget_path = sub / "budget.jsonl"
        spent = 0.0
        if budget_path.exists():
            with budget_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        spent += json.loads(line).get("amount_usd", 0.0)
        table.add_row(sub.name, proj.get("status", "?"), iter_str, best_str, f"{spent:.2f}")
    console.print(table)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@app.command("status")
def status(name: str = typer.Argument(...)):
    """Print a one-screen status block for a project. Same shape as the
    /status slash command but available outside Claude Code."""
    target = _projects_dir() / name
    if not target.exists():
        console.print(f"[red]Project {name!r} not found.[/red]")
        raise typer.Exit(1)
    proj = json.loads((target / "PROJECT.json").read_text(encoding="utf-8")) if (target / "PROJECT.json").exists() else {}
    console.print(f"PROJECT  [cyan]{name}[/cyan]    STATUS  {proj.get('status','?')}")
    mp = target / "MISSION.json"
    if mp.exists():
        m = json.loads(mp.read_text(encoding="utf-8"))
        primary = next((c for c in m["success_criteria"] if c.get("is_primary")), m["success_criteria"][0])
        console.print(f"MISSION  {primary['metric']} {primary['operator']} {primary['target']}    FLOOR {primary['operational_floor']}")
    rsp = target / "RUN_STATE.json"
    if rsp.exists():
        rs = json.loads(rsp.read_text(encoding="utf-8"))
        console.print(f"ITER     {rs.get('current_iteration', 0)}    TERMINATED  {rs.get('terminated', False)}")


# ---------------------------------------------------------------------------
# library
# ---------------------------------------------------------------------------

@app.command("library")
def library(
    section: str = typer.Argument("all", help="prompt_patterns | failure_modes | rag_recipes | model_routes | judge_templates | all"),
    tag: Optional[str] = typer.Option(None, "--tag", help="Filter to entries with this tag."),
):
    """Inspect the framework's cross-project knowledge library."""
    files = {
        "prompt_patterns":  "prompt_pattern_library.jsonl",
        "failure_modes":    "failure_modes.jsonl",
        "rag_recipes":      "rag_recipes.jsonl",
        "model_routes":     "model_route_priors.jsonl",
        "judge_templates":  "eval_judge_templates.jsonl",
    }
    chosen = list(files.items()) if section == "all" else [(section, files.get(section, ""))]
    root = _framework_root() / "knowledge"
    for label, fname in chosen:
        p = root / fname
        if not p.exists():
            continue
        console.rule(label)
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if tag and tag not in rec.get("tags", []):
                    continue
                # Pull a couple of headline fields for compactness.
                head = rec.get("pattern_id") or rec.get("failure_id") or rec.get("recipe_id") or rec.get("prior_id") or rec.get("template_id") or "?"
                tags = ",".join(rec.get("tags", [])[:4])
                console.print(f"  [cyan]{head}[/cyan]  [{tags}]")


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

@app.command("replay")
def replay(name: str = typer.Argument(...)):
    """Re-execute every TrialResult in a project's experiment_log.jsonl and
    diff against the recorded metrics. Useful for verifying determinism
    and detecting framework-version drift.

    This delegates to tools/replay_runner.py to keep CLI imports light.
    """
    target = _projects_dir() / name
    if not target.exists():
        console.print(f"[red]Project {name!r} not found.[/red]")
        raise typer.Exit(1)
    from importlib import util
    runner_path = _framework_root() / "tools" / "replay_runner.py"
    spec = util.spec_from_file_location("replay_runner", runner_path)
    mod = util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    mod.run(target)


if __name__ == "__main__":
    app()
