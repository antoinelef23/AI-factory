"""Make a scaffolded app a project Claudo can build in.

Claudo's orchestrator expects its PROJECT to be a git repository (it commits per task, scoped to the
task's files), to carry a `justfile` with an `evals` recipe (the merge gate), and a `pyproject.toml`.
The golden path provides the last two; the git repo is created here, with a LOCAL identity so that the
commits made by agents never borrow the operator's personal git configuration.
"""

from __future__ import annotations

import re
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


# Claudo's runtime state is never delivered: a signed approval token committed to git can be copied back into
# `.approvals/` and replayed, and the lock and state files mean nothing elsewhere. `journal.jsonl` and the
# `*-review.md` reports stay tracked: they are the audit trail and carry no secret.
RUNTIME_IGNORES = (
    "work/*/.approvals/",
    "work/*/.runs/orchestrator.lock",
    "work/*/.runs/state.json",
)

UNTRACK_RUNTIME_COMMIT = """chore: stop tracking Claudo runtime state

Why: approval tokens, the orchestrator lock and its state file are runtime state, not deliverables. A signed
token kept in git history could be replayed.

Run: auto
"""


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
    changed |= _ignore_runtime_state(app)
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
    else:
        changed |= _untrack_runtime_state(app)
    return changed


def _ignore_runtime_state(app: Path) -> bool:
    """Idempotently add RUNTIME_IGNORES to the app's .gitignore. True if the file changed."""
    path = app / ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    have = {ln.strip() for ln in existing.splitlines()}
    missing = [p for p in RUNTIME_IGNORES if p not in have]
    if not missing:
        return False
    text = existing if not existing or existing.endswith("\n") else existing + "\n"
    path.write_text(text + "\n".join(missing) + "\n", encoding="utf-8", newline="\n")
    return True


def _untrack_runtime_state(app: Path) -> bool:
    """Stop tracking runtime state a previous run committed (files stay on disk). True if committed."""
    tracked = [
        f for f in _git_raw(app, "ls-files", "-z", "--", "work").split("\0") if f and _is_runtime_path(f)
    ]
    if not tracked:
        return False
    _git(app, "rm", "-q", "--cached", "--", *tracked)
    # the new .gitignore rides along: it is what keeps these paths untracked
    _git(app, "add", "--", ".gitignore")
    _git(app, "commit", "-q", "-m", UNTRACK_RUNTIME_COMMIT)
    return True


def _is_runtime_path(rel: str) -> bool:
    parts = rel.split("/")
    if len(parts) < 4 or parts[0] != "work":
        return False
    if parts[2] == ".approvals":
        return True
    return parts[2] == ".runs" and len(parts) == 4 and parts[3] in ("orchestrator.lock", "state.json")


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


SCOPE_DRIFT_COMMIT = """chore({slug}): edits outside every task scope

Why: these files were changed by the build but belong to no task's files_touched, so Claudo's scoped commits
skipped them. They are committed here, apart from the bookkeeping and flagged for IT, who must
acknowledge them before shipping: nobody planned or traced them.

Scope-Drift: {files}
Run: auto
"""


def _commit_paths(app: Path, paths: list[str], message: str) -> None:
    shown = ", ".join(paths[:12]) + (f" (+{len(paths) - 12} more)" if len(paths) > 12 else "")
    _git(app, "add", "-A", "--", *paths)
    _git(app, "commit", "-q", "-m", message.replace("{files}", shown), "--", *paths)


def commit_leftovers_split(app: Path, slug: str) -> tuple[list[str], list[str]]:
    """Commit what the build left uncommitted, in two commits. Returns (bookkeeping, scope drift).

    Bookkeeping is the item's own folder (work/<slug>/: run log, journal, reports); it is expected. Everything
    else was changed outside every task's scope: it gets its own commit so the diff shows it, and the caller
    flags it for IT."""
    files = porcelain(app)
    bookkeeping = [f for f in files if f.startswith(f"work/{slug}/")]
    drift = [f for f in files if f not in bookkeeping]
    if bookkeeping:
        _commit_paths(app, bookkeeping, LEFTOVERS_COMMIT.replace("{slug}", slug))
    if drift:
        _commit_paths(app, drift, SCOPE_DRIFT_COMMIT.replace("{slug}", slug))
    return bookkeeping, drift


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


def remote_url(app: Path, name: str = "origin") -> str:
    p = subprocess.run(
        ["git", "remote", "get-url", name], cwd=app, capture_output=True, text=True, encoding="utf-8"
    )
    return p.stdout.strip() if p.returncode == 0 else ""


def add_remote(app: Path, url: str, name: str = "origin") -> None:
    """Point `name` at `url`. An existing remote with a DIFFERENT url is refused, never overwritten."""
    existing = remote_url(app, name)
    if existing and existing != url:
        raise ProjectError(f"remote '{name}' of {app} already points to {existing}: refusing to repoint it")
    if not existing:
        _git(app, "remote", "add", name, url)


def push_branch(app: Path, branch: str, remote: str = "origin") -> None:
    _git(app, "push", "-q", "-u", remote, branch)


def sync_merged_base(app: Path, slug: str, base: str) -> str:
    """After IT merged the pull request on the host: fast-forward the local base to it and drop the change
    branch. Fast-forward only, like the local merge. Returns the new base tip."""
    if porcelain(app):
        raise ProjectError(f"{app} has uncommitted changes: commit or discard them before syncing")
    _git(app, "fetch", "-q", "origin")
    before = current_branch(app)
    _git(app, "switch", "-q", base)
    try:
        _git(app, "merge", "--ff-only", f"origin/{base}")
    except ProjectError as e:
        _git(app, "switch", "-q", before)  # leave the app where it was, like merge_fast_forward
        raise ProjectError(
            f"local {base} has diverged from origin/{base}: resolve it by hand, then sync again"
        ) from e
    branch = f"factory/{slug}"
    if branch_exists(app, branch):
        _git(app, "branch", "-q", "-D", branch)  # its work is in the merged base now
    return _git(app, "rev-parse", "HEAD")


# A row Claudo appends to tasks.md for each node: | 2026-10-08 07:09 | CP-1 | owner | checkpoint validated | |
RUN_LOG_ROW = re.compile(r"^\| \d{4}-\d{2}-\d{2} \d{2}:\d{2} \|")


def head_sha(app: Path) -> str:
    """HEAD of the app's OWN git repository ("" when it is not one: never a parent repository's HEAD)."""
    if not (app / ".git").exists():
        return ""
    p = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", "HEAD"],
        cwd=app,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return p.stdout.strip() if p.returncode == 0 else ""


def post_approval_changes(app: Path, slug: str, since: str) -> list[str]:
    """What changed between `since` (the approved HEAD) and HEAD beyond Claudo's own bookkeeping.

    Acceptable: anything under work/<slug>/.runs/ (journal, reports, state) and run-log rows appended to
    work/<slug>/tasks.md. Anything else was never gated and never seen by IT."""
    problems: list[str] = []
    for name in _git(app, "diff", "--name-only", since, "HEAD").splitlines():
        if name.startswith(f"work/{slug}/.runs/"):
            continue
        if name == f"work/{slug}/tasks.md":
            diff = _git_raw(app, "diff", "-U0", since, "HEAD", "--", name)
            for line in diff.splitlines():
                if line.startswith(("+++", "---", "@@", "diff ", "index ")):
                    continue
                if line.startswith("+") and RUN_LOG_ROW.match(line[1:]):
                    continue
                problems.append(f"{name} (edited beyond run-log rows)")
                break
            continue
        problems.append(name)
    return problems


def ref_exists(app: Path, ref: str) -> bool:
    return (
        subprocess.run(["git", "rev-parse", "--verify", "-q", ref], cwd=app, capture_output=True).returncode
        == 0
    )


def rev_parse(app: Path, ref: str) -> str:
    return _git(app, "rev-parse", ref)


def modified_tests(app: Path, base_sha: str) -> list[str]:
    """Existing test files (tests/) a change modified, renamed or deleted since `base_sha`, as `path (how)`.

    Adding tests is what a change should do; rewriting the ones that were there can hide a regression."""
    try:
        out = _git(app, "diff", "--name-status", "-M", base_sha, "HEAD", "--", "tests")
    except ProjectError:
        return []
    changed: list[str] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0][:1] in "MDRT":
            changed.append(f"{parts[1]} ({'deleted' if parts[0][:1] == 'D' else 'modified'})")
    return changed
