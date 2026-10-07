"""Make a scaffolded app a project Claudo can build in.

Claudo's orchestrator expects its PROJECT to be a git repository (it commits per task, scoped to the
task's files), to carry a `justfile` with an `evals` recipe (the merge gate), and a `pyproject.toml`.
The golden path provides the last two; the git repo is created here, with a LOCAL identity so that the
commits made by agents never borrow the operator's personal git configuration.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

IDENTITY = ("AI Factory", "factory@localhost")

DEFAULT_JUSTFILE = """# Standard targets (IT golden path). Claudo's eval gate runs `just evals`.
test:
    uv run pytest -q

evals:
    uv run pytest -q -m eval

lint:
    uv run ruff check .
"""

INITIAL_COMMIT = """chore: scaffold from the IT golden path

Why: the app starts from the company golden path so CI, Dockerfile and layout are IT's, not the agent's.

Run: auto
"""


class ProjectError(Exception):
    pass


def _git(app: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", *args], cwd=app, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if p.returncode != 0:
        raise ProjectError(f"git {' '.join(args)} failed in {app}: {(p.stderr or p.stdout).strip()[-300:]}")
    return p.stdout.strip()


def has_recipe(justfile: Path, name: str) -> bool:
    if not justfile.is_file():
        return False
    return any(
        line.startswith(f"{name}:") or line.startswith(f"{name} ")
        for line in justfile.read_text(encoding="utf-8").splitlines()
    )


def prepare_project(app: Path) -> bool:
    """Idempotently turn `app` into a Claudo project. Returns True if anything was set up."""
    changed = False
    justfile = app / "justfile"
    if not has_recipe(justfile, "evals"):
        # keep an IT-provided justfile: add only the missing recipe
        existing = justfile.read_text(encoding="utf-8") if justfile.is_file() else ""
        addition = "evals:\n    uv run pytest -q -m eval\n"
        justfile.write_text(
            (existing.rstrip() + "\n\n" + addition) if existing else DEFAULT_JUSTFILE,
            encoding="utf-8",
            newline="\n",
        )
        changed = True
    if not (app / ".git").exists():
        _git(app, "init", "-q", "-b", "main")
        changed = True
    # Local identity and no CRLF rewriting: the repo must behave the same on every OS.
    _git(app, "config", "user.name", IDENTITY[0])
    _git(app, "config", "user.email", IDENTITY[1])
    _git(app, "config", "core.autocrlf", "false")
    has_commit = (
        subprocess.run(
            ["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=app, capture_output=True
        ).returncode
        == 0
    )
    if not has_commit:
        _git(app, "add", "-A")
        _git(app, "commit", "-q", "-m", INITIAL_COMMIT)
        changed = True
    return changed
