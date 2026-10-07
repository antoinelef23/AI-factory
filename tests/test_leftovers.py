import shutil
import subprocess
from pathlib import Path

import pytest

from factory.claudo import BuildResult
from factory.project import commit_leftovers, porcelain, prepare_project
from tests.test_claudo_build import PlanThenBuildAgent, ScriptedClaudo

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


# ------------------------------------------------------------------ the helper


def test_a_clean_tree_commits_nothing(app):
    head = git(app, "rev-parse", "HEAD")
    assert porcelain(app) == [] and commit_leftovers(app, "x") == []
    assert git(app, "rev-parse", "HEAD") == head


def test_modified_and_untracked_files_are_committed_in_one_labelled_commit(app):
    (app / "README.md").write_text("# changed\n", encoding="utf-8")  # tracked, modified
    (app / "app" / "new.py").write_text("x = 1\n", encoding="utf-8")  # untracked
    files = commit_leftovers(app, "demo")
    assert sorted(files) == ["README.md", "app/new.py"] and porcelain(app) == []
    subject = git(app, "log", "-1", "--format=%s")
    body = git(app, "log", "-1", "--format=%b")
    assert subject == "chore(demo): commit work left outside the task scopes"
    assert "Why:" in body and "README.md" in body and "Run: auto" in body
    assert git(app, "log", "-1", "--format=%an") == "AI Factory"


def test_gitignored_files_are_never_committed(app):
    (app / ".venv").mkdir()
    (app / ".venv" / "junk.py").write_text("x", encoding="utf-8")
    (app / ".env").write_text("SECRET=1", encoding="utf-8")
    (app / "__pycache__").mkdir()
    (app / "__pycache__" / "a.pyc").write_bytes(b"\0")
    assert porcelain(app) == [] and commit_leftovers(app, "x") == []


def test_a_long_file_list_is_summarized_in_the_message(app):
    for i in range(20):
        (app / f"f{i}.txt").write_text(str(i), encoding="utf-8")
    assert len(commit_leftovers(app, "x")) == 20
    assert "(+8 more)" in git(app, "log", "-1", "--format=%b")


# ------------------------------------------------------------------ in the foreman


class LeavesWorkBehind(ScriptedClaudo):
    """Like Claudo in the live run: the task commits its own files, a file outside every scope stays dirty."""

    def run_build(self, slug, project, **kw):
        (project / "README.md").write_text(
            "# endpoints documented outside any task scope\n", encoding="utf-8"
        )
        return super().run_build(slug, project, **kw)


def to_ship_review(foreman, engine):
    foreman.runner, foreman.engine = PlanThenBuildAgent(), engine
    foreman.cfg.claudo_build_from = "poc"
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    return foreman.approve(foreman.approve(item, "business"), "owner")


def test_work_left_outside_the_task_scopes_is_committed_so_head_is_the_delivered_state(foreman):
    engine = LeavesWorkBehind()
    item = to_ship_review(foreman, engine)
    app = foreman.app_dir(item)
    assert item.stage == "ship_review" and porcelain(app) == []
    assert "endpoints documented outside" in git(app, "show", "HEAD:README.md")  # the regression: it WAS lost
    assert "chore(x): commit work left outside the task scopes" in git(app, "log", "--format=%s")
    assert any(h["event"] == "leftovers" and "README.md" in h["detail"] for h in item.history)


def test_the_final_run_after_ip_approval_also_leaves_a_clean_head(foreman):
    class FinalRunDirties(ScriptedClaudo):
        def run_build(self, slug, project, *, stop_at_checkpoint=True, **kw):
            if not stop_at_checkpoint:  # Claudo's bookkeeping after the approval
                (project / "work" / slug / ".runs" / "late.txt").parent.mkdir(parents=True, exist_ok=True)
                (project / "work" / slug / ".runs" / "late.txt").write_text("done", encoding="utf-8")
            return super().run_build(slug, project, stop_at_checkpoint=stop_at_checkpoint, **kw)

    engine = FinalRunDirties([BuildResult("checkpoint", "CP-1", "x"), BuildResult("done", log="ok")])
    item = to_ship_review(foreman, engine)
    item = foreman.approve(item, "it", by="bob")
    assert item.status == "shipped" and porcelain(foreman.app_dir(item)) == []


def test_the_clean_tree_gate_rejects_a_dirty_app(foreman):
    engine = ScriptedClaudo()
    item = to_ship_review(foreman, engine)
    app = foreman.app_dir(item)
    assert foreman._clean_tree_gate(item).ok
    (app / "app" / "late_edit.py").write_text("x = 1\n", encoding="utf-8")
    res = foreman._clean_tree_gate(item)
    assert not res.ok and "late_edit.py" in res.detail


def test_the_clean_tree_gate_does_not_apply_to_an_app_that_is_not_a_git_project(foreman):
    item = foreman.run(foreman.intake("X", "an api", "mvp"))
    item = foreman.approve(foreman.approve(item, "business"), "it")
    item = foreman.approve(item, "owner")  # single-agent offline build: no git repo
    assert item.status == "waiting"
    assert "not applicable: the app is not a git project" in foreman.store.read(item, "gate-report.md")


def test_clean_tree_is_an_mvp_and_prod_gate_only(factory_root):
    from factory.config import load_config

    cfg = load_config(factory_root)
    assert "clean_tree" in cfg.gates_for("mvp") and "clean_tree" in cfg.gates_for("prod")
    assert "clean_tree" not in cfg.gates_for("poc") and "clean_tree" not in cfg.gates_for("pov")
