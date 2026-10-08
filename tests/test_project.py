import shutil
import subprocess
from pathlib import Path

import pytest

from factory.project import DEFAULT_JUSTFILE, has_recipe, prepare_project

REPO = Path(__file__).resolve().parents[1]


def git(app, *args):
    return subprocess.run(
        ["git", *args], cwd=app, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


@pytest.fixture
def app(tmp_path):
    dest = tmp_path / "app"
    shutil.copytree(REPO / "golden_paths" / "python-fastapi", dest)
    return dest


def test_prepares_a_git_repo_with_a_local_identity_and_one_initial_commit(app):
    assert prepare_project(app) is True
    assert (
        git(app, "log", "--format=%an <%ae>|%s")
        == "AI Factory <factory@localhost>|chore: scaffold from the IT golden path"
    )
    assert git(app, "status", "--short") == ""  # everything committed
    assert git(app, "config", "--local", "user.email") == "factory@localhost"
    assert git(app, "config", "--local", "core.autocrlf") == "false"


def test_the_commit_does_not_borrow_the_operators_global_identity(app, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(app.parent / "global"))
    (app.parent / "global").write_text("[user]\n\tname = Personal Person\n\temail = me@personal.example\n")
    prepare_project(app)
    assert "Personal" not in git(app, "log", "--format=%an %ae")


def test_adds_the_evals_recipe_claudo_needs(app):
    (app / "justfile").unlink(missing_ok=True)
    prepare_project(app)
    text = (app / "justfile").read_text(encoding="utf-8")
    assert text == DEFAULT_JUSTFILE and has_recipe(app / "justfile", "evals")


def test_keeps_an_it_justfile_and_only_adds_the_missing_recipe(app):
    (app / "justfile").write_text("test:\n    make test\n", encoding="utf-8")
    prepare_project(app)
    text = (app / "justfile").read_text(encoding="utf-8")
    assert text.startswith("test:\n    make test\n") and "evals:\n    uv run pytest -q -m eval" in text


def test_is_idempotent_and_never_rewrites_history_or_discards_work(app):
    prepare_project(app)
    head = git(app, "rev-parse", "HEAD")
    (app / "app" / "work.py").write_text("x = 1\n", encoding="utf-8")  # uncommitted agent work
    assert prepare_project(app) is False
    assert git(app, "rev-parse", "HEAD") == head and (app / "app" / "work.py").is_file()


def test_golden_path_ships_the_eval_marker_and_recipes():
    gp = REPO / "golden_paths" / "python-fastapi"
    assert has_recipe(gp / "justfile", "evals") and has_recipe(gp / "justfile", "test")
    assert "eval:" in (gp / "pyproject.toml").read_text(encoding="utf-8")  # registered pytest marker


# ------------------------------------------------------------------ Claudo runtime state is never delivered


def test_prepare_project_ignores_claudo_runtime_state_once(app):
    prepare_project(app)
    prepare_project(app)
    lines = (app / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in ("work/*/.approvals/", "work/*/.runs/orchestrator.lock", "work/*/.runs/state.json"):
        assert lines.count(pattern) == 1


def test_runtime_state_already_tracked_is_untracked_but_kept_on_disk(app):
    prepare_project(app)
    token = app / "work" / "x" / ".approvals" / "CP-1.handled-1"
    state = app / "work" / "x" / ".runs" / "state.json"
    journal = app / "work" / "x" / ".runs" / "journal.jsonl"
    for f in (token, state, journal):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x\n", encoding="utf-8")
    git(app, "add", "-f", "-A")  # a run (or an older factory) committed them
    git(app, "commit", "-q", "-m", "tracked by mistake")
    assert prepare_project(app) is True
    tracked = git(app, "ls-files").splitlines()
    assert "work/x/.approvals/CP-1.handled-1" not in tracked and "work/x/.runs/state.json" not in tracked
    assert "work/x/.runs/journal.jsonl" in tracked  # the audit trail stays
    assert token.is_file() and state.is_file()
    assert git(app, "log", "-1", "--format=%s") == "chore: stop tracking Claudo runtime state"
    assert prepare_project(app) is False  # idempotent


def test_leftovers_never_commit_a_token(app):
    from factory.project import commit_leftovers

    prepare_project(app)
    token = app / "work" / "x" / ".approvals" / "CP-1"
    token.parent.mkdir(parents=True)
    token.write_text("approved_by=me\n", encoding="utf-8")
    (app / "other.txt").write_text("x\n", encoding="utf-8")
    assert commit_leftovers(app, "x") == ["other.txt"]
    assert "work/x/.approvals/CP-1" not in git(app, "ls-files")
