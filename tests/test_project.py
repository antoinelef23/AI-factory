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
