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
    assert item.claudo_cp_consumed and not any(a["role"] == "it" for a in item.approvals)
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


# ------------------------------------------------------------- J-1: rejecting after a re-decision reworks


def test_rejecting_after_a_redecision_hands_the_reason_to_claudo_and_reopens_its_checkpoint(foreman):
    engine = Claudo("BLOCK", after_approval=rewrite_review("WARN"))
    engine.outcomes.append(BuildResult("checkpoint", "CP-1", "reworked"))  # the rework pauses again
    item = at_ship_review(foreman, engine)
    app = foreman.app_dir(item)
    state = app / "work" / "x" / ".runs" / "state.json"
    state.write_text(
        '{"T1": "done", "CP-1": "done"}', encoding="utf-8"
    )  # what Claudo leaves after its final run
    item = foreman.approve(item, "it", by="bob", note="knowing")
    assert item.status == "waiting" and item.claudo_cp_consumed
    item = foreman.reject(item, "it", "the WARN is right: do not touch existing endpoints", by="bob")
    assert item.claudo_rejection["reason"].startswith("the WARN is right")
    item = foreman.run(item)
    assert engine.reopened == ["CP-1"] and "CP-1" not in state.read_text(encoding="utf-8")
    assert engine.rejected == [("CP-1", "the WARN is right: do not touch existing endpoints", "bob")]
    assert (item.stage, item.status) == ("ship_review", "waiting") and not item.claudo_cp_consumed


def test_approving_after_a_redecision_does_not_run_claudo_again_and_clears_the_checkpoint(foreman):
    engine = Claudo("BLOCK", after_approval=rewrite_review("WARN"))
    item = foreman.approve(at_ship_review(foreman, engine), "it", by="bob", note="knowing")
    runs = len(engine.runs)
    done = foreman.approve(item, "it", by="bob", note="read the WARN, acceptable")
    assert done.status == "shipped" and len(engine.runs) == runs
    assert done.claudo_cp == "" and not done.claudo_cp_consumed


def test_reopen_checkpoint_removes_only_that_node_from_claudos_state(tmp_path):
    state = tmp_path / "work" / "x" / ".runs" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"T1": "done", "CP-1": "done"}', encoding="utf-8")
    assert ClaudoEngine.reopen_checkpoint(tmp_path, "x", "CP-1") is True
    assert state.read_text(encoding="utf-8") == '{"T1": "done"}'
    assert ClaudoEngine.reopen_checkpoint(tmp_path, "x", "CP-1") is False  # nothing left to reopen
    assert ClaudoEngine.reopen_checkpoint(tmp_path, "missing", "CP-1") is False


# ------------------------------------------------------------- J-7: an item paused before nonces existed


def test_an_item_paused_before_nonces_existed_gets_one_at_approval(foreman):
    engine = Claudo("PASS")
    item = at_ship_review(foreman, engine)
    item.approval_nonce = ""  # paused at CP-1 before the upgrade
    done = foreman.approve(item, "it", by="bob")
    final_env = engine.runs[-1]["env"]
    assert done.status == "shipped" and final_env["LAB_APPROVAL_NONCE"]
    assert engine.signed[-1]["nonce"] == final_env["LAB_APPROVAL_NONCE"] == done.approval_nonce


# ------------------------------------------------------------- a block after the final run is reworkable


def test_a_ship_blocked_after_the_final_run_is_reworked_by_a_rejection(foreman):
    def sneaky(project):
        (project / "app" / "main.py").write_text("# edited after the approval\n", encoding="utf-8")

    engine = Claudo(after_approval=sneaky)
    engine.outcomes.append(BuildResult("checkpoint", "CP-1", "reworked"))
    item = at_ship_review(foreman, engine)
    (foreman.app_dir(item) / "work" / "x" / ".runs" / "state.json").write_text(
        '{"CP-1": "done"}', encoding="utf-8"
    )
    item = foreman.approve(item, "it", by="bob")
    assert item.status == "blocked" and item.claudo_cp_consumed
    item = foreman.run(foreman.reject(item, "it", "revert the edit made after the approval", by="bob"))
    assert engine.reopened == ["CP-1"] and engine.rejected[-1][1] == "revert the edit made after the approval"
    assert (item.stage, item.status) == ("ship_review", "waiting")


# ------------------------------------------------------------- J-8: an older Claudo is reported, not trusted


def test_an_older_claudo_without_nonces_is_reported_on_the_item(foreman):

    engine = Claudo("PASS")
    engine.nonce_support = False
    item = at_ship_review(foreman, engine)
    assert any("replay protection unavailable" in n for n in item.notes)
    assert [h["event"] for h in item.history].count("warning") == 1


def test_supports_nonce_reads_the_engines_own_approvals_module(tmp_path):
    engine = ClaudoEngine.__new__(ClaudoEngine)
    engine.home = tmp_path
    assert engine.supports_nonce() is False  # no approvals.py at all
    approvals = tmp_path / "lab" / "engine" / "approvals.py"
    approvals.parent.mkdir(parents=True)
    approvals.write_text('ENV_SECRET = "LAB_APPROVAL_SECRET"\n', encoding="utf-8")
    assert engine.supports_nonce() is False  # before b9b96c7
    approvals.write_text('ENV_SECRET = "x"\nENV_NONCE = "LAB_APPROVAL_NONCE"\n', encoding="utf-8")
    assert engine.supports_nonce() is True


# ------------------------------------------------------------- J-3: refusals name a command that helps


def test_the_refusal_after_a_post_approval_block_names_reject_and_reject_regates(foreman):
    from factory.foreman import FactoryError
    from factory.project import head_sha

    def sneaky(project):
        (project / "app" / "main.py").write_text("# edited after the approval\n", encoding="utf-8")

    engine = Claudo(after_approval=sneaky)
    engine.outcomes.append(BuildResult("checkpoint", "CP-1", "reworked"))
    item = at_ship_review(foreman, engine)
    item = foreman.approve(item, "it", by="bob")
    with pytest.raises(FactoryError) as err:
        foreman.approve(item, "it", by="bob", note="again")
    assert "factory reject x --as it" in str(err.value) and "factory run" not in str(err.value)
    item = foreman.run(foreman.reject(item, "it", "revert it", by="bob"))  # follow the advice
    assert (item.stage, item.status) == ("ship_review", "waiting")
    assert item.gated_sha == head_sha(foreman.app_dir(item))  # gated again, on the new head
