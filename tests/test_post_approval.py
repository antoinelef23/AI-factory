"""After IT approves, Claudo's final run may change nothing but its own bookkeeping (review finding C4)."""

import pytest

from factory.claudo import BuildResult, ClaudoEngine
from factory.project import head_sha, post_approval_changes, prepare_project
from tests.test_claudo_build import PlanThenBuildAgent, ScriptedClaudo
from tests.test_leftovers import git

REPORT = "work/x/.runs/CP-1-review.md"
RUN_LOG_ROW = "| 2026-10-08 07:09 | CP-1 | owner | checkpoint validated | |\n"


class Claudo(ScriptedClaudo):
    """Writes a real review report at the checkpoint; `after_approval(project)` is what the final run does."""

    def __init__(self, verdict="PASS", after_approval=None):
        super().__init__([BuildResult("checkpoint", "CP-1", "x"), BuildResult("done", log="ok")])
        self.verdict, self.after_approval = verdict, after_approval

    def review_verdict(self, project, slug, cp):  # read the report like the real bridge does
        return ClaudoEngine.review_verdict(project, slug, cp)

    def run_build(self, slug, project, *, stop_at_checkpoint=True, **kw):
        report = project / REPORT
        if stop_at_checkpoint:
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(f"# Review CP-1: aggregated verdict: {self.verdict}\n", encoding="utf-8")
        elif self.after_approval:
            self.after_approval(project)
        return super().run_build(slug, project, stop_at_checkpoint=stop_at_checkpoint, **kw)


def at_ship_review(foreman, engine):
    foreman.runner, foreman.engine = PlanThenBuildAgent(), engine
    foreman.cfg.claudo_build_from = "poc"
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    return foreman.approve(foreman.approve(item, "business"), "owner")


# ------------------------------------------------------------------ what may change after the approval


def test_bookkeeping_only_after_the_approval_ships_and_moves_the_approved_head(foreman):
    def bookkeeping(project):
        with (project / "work" / "x" / "tasks.md").open("a", encoding="utf-8") as f:
            f.write(RUN_LOG_ROW)
        (project / "work" / "x" / ".runs").mkdir(parents=True, exist_ok=True)
        (project / "work" / "x" / ".runs" / "journal.jsonl").write_text('{"event": "x"}\n', encoding="utf-8")

    item = at_ship_review(foreman, Claudo(after_approval=bookkeeping))
    app = foreman.app_dir(item)
    before = head_sha(app)
    done = foreman.approve(item, "it", by="bob")
    assert (done.stage, done.status) == ("shipped", "shipped"), done.feedback
    assert done.approved_head == head_sha(app) != before  # bookkeeping was committed, and is the new head


def test_a_production_edit_after_the_approval_blocks_the_ship(foreman):
    def sneaky(project):
        (project / "app" / "main.py").write_text("# edited after the approval\n", encoding="utf-8")

    item = at_ship_review(foreman, Claudo(after_approval=sneaky))
    done = foreman.approve(item, "it", by="bob")
    assert (done.stage, done.status) == ("ship_review", "blocked")
    assert "app/main.py" in done.feedback and "never gated" in done.feedback
    assert not any(a["role"] == "it" for a in done.approvals)  # the approval was not recorded


def test_editing_the_plan_after_the_approval_blocks_the_ship(foreman):
    def rewrite_plan(project):
        path = project / "work" / "x" / "tasks.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace("Implement", "Do something else"), encoding="utf-8"
        )

    item = at_ship_review(foreman, Claudo(after_approval=rewrite_plan))
    done = foreman.approve(item, "it", by="bob")
    assert done.status == "blocked" and "tasks.md (edited beyond run-log rows)" in done.feedback


def test_post_approval_changes_helper_distinguishes_log_rows_from_edits(foreman):
    item = at_ship_review(foreman, ScriptedClaudo())
    app = foreman.app_dir(item)
    since = head_sha(app)
    tasks = app / "work" / "x" / "tasks.md"
    tasks.write_text(tasks.read_text(encoding="utf-8") + RUN_LOG_ROW, encoding="utf-8", newline="\n")
    git(app, "commit", "-qam", "log row")
    assert post_approval_changes(app, "x", since) == []
    (app / "README.md").write_text("changed\n", encoding="utf-8")
    git(app, "commit", "-qam", "readme")
    assert post_approval_changes(app, "x", since) == ["README.md"]


# ------------------------------------------------------------------ a verdict that moved goes back to IT


def rewrite_review(verdict):
    def hook(project):
        (project / REPORT).write_text(
            f"# Review CP-1: aggregated verdict: {verdict}\nrerun\n", encoding="utf-8"
        )

    return hook


def test_a_review_that_changed_to_another_non_pass_verdict_sends_the_decision_back(foreman):
    engine = Claudo("BLOCK", after_approval=rewrite_review("WARN"))
    item = at_ship_review(foreman, engine)
    item = foreman.approve(item, "it", by="bob", note="accepted the BLOCK knowingly")
    assert (item.stage, item.status) == ("ship_review", "waiting")
    assert "re-ran after your approval" in item.feedback and "WARN" in item.feedback
    assert item.claudo_cp == "" and not any(a["role"] == "it" for a in item.approvals)
    assert item.claudo_review["verdict"] == "WARN"  # the verdict that now stands


def test_after_a_redecision_the_new_approval_needs_a_note_and_runs_claudo_no_more(foreman):
    engine = Claudo("BLOCK", after_approval=rewrite_review("WARN"))
    item = at_ship_review(foreman, engine)
    item = foreman.approve(item, "it", by="bob", note="knowing")
    assert item.status == "waiting"
    runs_before = len(engine.runs)
    from factory.foreman import FactoryError

    with pytest.raises(FactoryError, match="reviewer said WARN"):
        foreman.approve(item, "it", by="bob")
    shipped = foreman.approve(item, "it", by="bob", note="read the new report")
    assert shipped.status == "shipped" and len(engine.runs) == runs_before  # no second orchestrator run


def test_an_unchanged_review_does_not_trigger_a_second_decision(foreman):
    item = at_ship_review(foreman, Claudo("PASS"))
    assert foreman.approve(item, "it", by="bob").status == "shipped"


def test_a_non_claudo_item_records_its_gated_head_when_approved(foreman):
    from factory.agents import AgentResult  # noqa: F401  (single-agent path: no Claudo)

    item = foreman.run(foreman.intake("Y", "an api", "mvp"))
    item = foreman.approve(foreman.approve(item, "business"), "it")
    item = foreman.approve(item, "owner")
    app = foreman.app_dir(item)
    prepare_project(app)
    item.approved_head = ""
    # offline build: the app is not a git project until prepared; the helper must never borrow a parent repo
    assert head_sha(foreman.cfg.apps_dir / "nowhere") == ""
