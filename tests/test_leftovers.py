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
    assert "chore(x): edits outside every task scope" in git(app, "log", "--format=%s")  # its own commit
    assert any(h["event"] == "scope_drift" and "README.md" in h["detail"] for h in item.history)


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


# ------------------------------------------------------------------ scope drift needs IT's ack (finding C3)


def test_split_commits_keep_bookkeeping_and_scope_drift_apart(app):
    from factory.project import commit_leftovers_split

    (app / "work" / "x").mkdir(parents=True)
    (app / "work" / "x" / "tasks.md").write_text("| run log |\n", encoding="utf-8")
    (app / "app" / "main.py").write_text("x = 2\n", encoding="utf-8")
    bookkeeping, drift = commit_leftovers_split(app, "x")
    assert bookkeeping == ["work/x/tasks.md"] and drift == ["app/main.py"] and porcelain(app) == []
    subjects = git(app, "log", "-2", "--format=%s").splitlines()
    assert subjects == [
        "chore(x): edits outside every task scope",
        "chore(x): commit work left outside the task scopes",
    ]
    assert "Scope-Drift: app/main.py" in git(app, "log", "-1", "--format=%b")
    assert "work/x/tasks.md" not in git(app, "show", "--name-only", "--format=", "HEAD")


def test_only_bookkeeping_leftovers_raise_no_acknowledgement(foreman):
    class OnlyBookkeeping(ScriptedClaudo):
        def run_build(self, slug, project, **kw):
            (project / "work" / slug / ".runs").mkdir(parents=True, exist_ok=True)
            (project / "work" / slug / ".runs" / "extra.log").write_text("x", encoding="utf-8")
            return super().run_build(slug, project, **kw)

    item = to_ship_review(
        foreman, OnlyBookkeeping([BuildResult("checkpoint", "CP-1", "x"), BuildResult("done", log="ok")])
    )
    assert item.ship_acks == []
    assert foreman.approve(item, "it", by="bob").status == "shipped"  # no --note needed


def test_scope_drift_blocks_the_approval_until_it_justifies_it(foreman):
    from factory.foreman import FactoryError

    item = to_ship_review(
        foreman, LeavesWorkBehind([BuildResult("checkpoint", "CP-1", "x"), BuildResult("done", log="ok")])
    )
    assert [a["kind"] for a in item.ship_acks] == ["scope_drift"] and "README.md" in item.ship_acks[0][
        "detail"
    ]
    with pytest.raises(FactoryError, match=r"scope_drift: README\.md.*--note"):
        foreman.approve(item, "it", by="bob")
    assert item.status == "waiting"  # nothing was signed
    done = foreman.approve(item, "it", by="bob", note="README addition is fine")
    assert done.status == "shipped" and done.approvals[-1]["note"] == "README addition is fine"


def test_a_non_pass_verdict_and_scope_drift_are_both_listed_in_the_refusal(foreman):
    from factory.foreman import FactoryError

    engine = LeavesWorkBehind()
    engine.review = ("WARN", "work/x/.runs/CP-1-review.md")
    item = to_ship_review(foreman, engine)
    with pytest.raises(FactoryError) as err:
        foreman.approve(item, "it", by="bob")
    assert "reviewer said WARN" in str(err.value) and "scope_drift" in str(err.value)


def test_the_acknowledgement_is_shown_to_it_and_in_the_pull_request(foreman, capsys):
    from factory.cli import _print_item

    item = to_ship_review(foreman, LeavesWorkBehind())
    _print_item(foreman, item)
    assert "needs IT: scope_drift: README.md" in capsys.readouterr().out
    assert "## Needs IT attention" in foreman._pr_body(item) and "README.md" in foreman._pr_body(item)


def test_a_retry_keeps_the_acknowledgements_of_the_commits_still_in_history(foreman):
    engine = LeavesWorkBehind(
        [BuildResult("checkpoint", "CP-1", "x"), BuildResult("checkpoint", "CP-1", "y")]
    )
    item = to_ship_review(foreman, engine)
    assert item.build_attempts == 1 and len(item.ship_acks) == 1
    item.build_attempts, item.claudo_cp = 2, ""  # a retry or a rework round re-enters Claudo
    foreman._do_build(item)
    assert len(item.ship_acks) == 1  # not cleared (and not duplicated)
