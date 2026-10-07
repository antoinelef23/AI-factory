import shutil
import subprocess
from pathlib import Path

import pytest

from factory.project import (
    ProjectError,
    begin_change,
    branch_exists,
    current_branch,
    merge_fast_forward,
    porcelain,
    prepare_project,
)

REPO = Path(__file__).resolve().parents[1]


def git(app, *args):
    return subprocess.run(
        ["git", *args], cwd=app, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


@pytest.fixture
def app(tmp_path):
    dest = tmp_path / "app"
    shutil.copytree(REPO / "golden_paths" / "python-fastapi", dest)
    prepare_project(dest)
    return dest


def commit(app, name="x.txt", text="x\n", msg="work"):
    (app / name).write_text(text, encoding="utf-8")
    git(app, "add", "-A")
    git(app, "commit", "-q", "-m", msg)


# ------------------------------------------------------------------ beginning a change


def test_a_clean_app_gets_a_change_branch_from_its_current_tip(app):
    head = git(app, "rev-parse", "HEAD")
    base, sha = begin_change(app, "fix-it")
    assert (base, sha) == ("main", head)
    assert current_branch(app) == "factory/fix-it" and git(app, "rev-parse", "HEAD") == head


def test_uncommitted_work_is_baselined_in_its_own_commit_so_the_change_diff_stays_clean(app):
    (app / "README.md").write_text("hand edit before the change\n", encoding="utf-8")
    base, sha = begin_change(app, "fix-it")
    assert porcelain(app) == [] and git(app, "log", "-1", "--format=%s", sha).startswith(
        "chore(fix-it): baseline"
    )
    assert (
        git(app, "rev-parse", "main") == sha
    )  # the baseline went onto the base branch, not the change branch


def test_an_app_that_is_not_a_repo_yet_becomes_one_with_a_baseline(tmp_path):
    dest = tmp_path / "plain"
    shutil.copytree(REPO / "golden_paths" / "python-fastapi", dest)
    assert not (dest / ".git").exists()
    base, sha = begin_change(dest, "first")
    assert (dest / ".git").is_dir() and current_branch(dest) == "factory/first" and sha


def test_beginning_twice_resumes_the_same_branch_and_keeps_the_original_base(app):
    base1, sha1 = begin_change(app, "fix-it")
    commit(app, msg="the change")
    base2, sha2 = begin_change(app, "fix-it")
    assert (base2, sha2) == (base1, sha1) and current_branch(app) == "factory/fix-it"


def test_a_second_change_cannot_start_on_top_of_an_unmerged_one(app):
    begin_change(app, "first")
    with pytest.raises(ProjectError, match="finish or merge"):
        begin_change(app, "second")


# ------------------------------------------------------------------ merging (the human's act)


def test_merge_fast_forwards_the_base_and_deletes_the_branch(app):
    begin_change(app, "fix-it")
    commit(app, "fix.txt", "fixed\n", "the fix")
    tip = git(app, "rev-parse", "HEAD")
    assert merge_fast_forward(app, "fix-it") == tip
    assert current_branch(app) == "main" and git(app, "rev-parse", "main") == tip
    assert (app / "fix.txt").is_file() and not branch_exists(app, "factory/fix-it")


def test_merge_refuses_when_the_base_moved_and_leaves_the_app_on_the_branch(app):
    begin_change(app, "fix-it")
    commit(app, "fix.txt", msg="the fix")
    git(app, "switch", "-q", "main")
    commit(app, "other.txt", msg="someone else moved main")
    git(app, "switch", "-q", "factory/fix-it")
    with pytest.raises(ProjectError, match="has moved.*cannot fast-forward"):
        merge_fast_forward(app, "fix-it")
    assert current_branch(app) == "factory/fix-it" and branch_exists(app, "factory/fix-it")
    assert not (app / "other.txt").exists()  # nothing was half-merged


def test_merge_refuses_a_dirty_tree(app):
    begin_change(app, "fix-it")
    commit(app)
    (app / "late.txt").write_text("late", encoding="utf-8")
    with pytest.raises(ProjectError, match="uncommitted"):
        merge_fast_forward(app, "fix-it")


def test_merge_of_a_missing_branch_is_an_error(app):
    with pytest.raises(ProjectError, match="no branch factory/ghost"):
        merge_fast_forward(app, "ghost")
