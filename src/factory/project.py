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


def _git_raw(app: Path, *args: str) -> str:
    """git's stdout exactly as printed: `status --porcelain` lines start with a significant space."""
    p = subprocess.run(
        ["git", *args], cwd=app, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if p.returncode != 0:
        raise ProjectError(f"git {' '.join(args)} failed in {app}: {(p.stderr or p.stdout).strip()[-300:]}")
    return p.stdout


def _git(app: Path, *args: str) -> str:
    return _git_raw(app, *args).strip()


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


LEFTOVERS_COMMIT = """chore({slug}): commit work left outside the task scopes

Why: Claudo's scoped commits skip files outside a task's files_touched (it flags them as scope
drift). The factory commits them here, separately and visibly, so HEAD is exactly the delivered
state and the diff shows what escaped the task scopes.

Artifacts: {files}
Run: auto
"""


def porcelain(app: Path) -> list[str]:
    """Paths with uncommitted changes (tracked or untracked, .gitignore honored); [] when clean."""
    out = _git_raw(app, "status", "--porcelain", "-uall")  # "XY path": never strip before slicing
    return [line[3:].strip().strip('"') for line in out.splitlines() if line.strip()]


def commit_all(app: Path, message: str) -> list[str]:
    """Commit everything uncommitted; "{files}" in `message` becomes the file list. Returns the files."""
    files = porcelain(app)
    if not files:
        return []
    _git(app, "add", "-A")
    shown = ", ".join(files[:12]) + (f" (+{len(files) - 12} more)" if len(files) > 12 else "")
    _git(app, "commit", "-q", "-m", message.replace("{files}", shown))
    return files


def commit_leftovers(app: Path, slug: str) -> list[str]:
    """Commit everything left uncommitted so that HEAD is the delivered state. Returns the files committed."""
    return commit_all(app, LEFTOVERS_COMMIT.replace("{slug}", slug))


BASELINE_COMMIT = """chore({slug}): baseline of the app before the change

Why: the change runs on its own branch, and a branch must start from a committed state. Anything
uncommitted in the app when the change begins is committed here, separately, so the change's own
diff contains only the change.

Run: auto
"""


def current_branch(app: Path) -> str:
    return _git(app, "rev-parse", "--abbrev-ref", "HEAD")


def branch_exists(app: Path, branch: str) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", "-q", f"refs/heads/{branch}"], cwd=app, capture_output=True
        ).returncode
        == 0
    )


def begin_change(app: Path, slug: str) -> tuple[str, str]:
    """Put the app on the change branch `factory/<slug>`. Returns (base branch, base sha).

    Idempotent: resuming a change that already has its branch switches back to it and keeps the base recorded
    at its first start (so `merge` knows what the branch started from). A dirty app is baselined first."""
    prepare_project(app)  # a git repo with a local identity and at least one commit
    branch = f"factory/{slug}"
    if branch_exists(app, branch):
        base = _git(app, "config", "--get", f"branch.{branch}.factory-base") or "main"
        _git(app, "switch", "-q", branch)
        reference = base if branch_exists(app, base) else "HEAD"
        return base, _git(app, "merge-base", reference, branch)
    base = current_branch(app)
    if base.startswith("factory/"):
        raise ProjectError(f"the app is on {base}: finish or merge that change before starting another")
    if porcelain(app):
        _git(app, "add", "-A")
        _git(app, "commit", "-q", "-m", BASELINE_COMMIT.format(slug=slug))
    sha = _git(app, "rev-parse", "HEAD")
    _git(app, "switch", "-q", "-c", branch)
    _git(app, "config", f"branch.{branch}.factory-base", base)
    return base, sha


def merge_fast_forward(app: Path, slug: str) -> str:
    """Fast-forward the base branch to the change branch (the human's explicit act: the factory never merges
    on its own). Refuses anything that is not a pure fast-forward. Returns the new base tip."""
    branch = f"factory/{slug}"
    if not branch_exists(app, branch):
        raise ProjectError(f"no branch {branch} in {app}")
    base = _git(app, "config", "--get", f"branch.{branch}.factory-base") or "main"
    if porcelain(app):
        raise ProjectError(f"{app} has uncommitted changes: commit or discard them before merging")
    _git(app, "switch", "-q", base)
    try:
        _git(app, "merge", "--ff-only", branch)
    except ProjectError as e:
        _git(app, "switch", "-q", branch)  # leave the app where it was
        raise ProjectError(
            f"{base} has moved since the change started, so {branch} cannot fast-forward: rebase the branch "
            f"onto {base} (or redo the change), then merge again"
        ) from e
    _git(app, "branch", "-q", "-d", branch)
    return _git(app, "rev-parse", "HEAD")


CHANGE_TRIPLET_COMMIT = """chore({slug}): add the spec, design and plan of the change

Why: the approved artifacts travel with the code they govern (work/{slug}/), on the change branch.

Run: auto
"""

CHANGE_BUILD_COMMIT = """feat({slug}): the {kind} built by the build agent

Why: a single build agent does not commit; the factory records its work as one commit on the change
branch so the diff against the base is exactly the change.

Artifacts: {files}
Run: auto
"""


def abandon_change(app: Path, slug: str) -> None:
    """Drop an unwanted change: back to the base branch, branch deleted. Refuses to lose uncommitted work."""
    branch = f"factory/{slug}"
    if not branch_exists(app, branch):
        return
    if porcelain(app):
        raise ProjectError(f"{app} has uncommitted changes: commit or discard them before abandoning")
    base = _git(app, "config", "--get", f"branch.{branch}.factory-base") or "main"
    if current_branch(app) == branch:
        _git(app, "switch", "-q", base)
    _git(app, "branch", "-q", "-D", branch)
