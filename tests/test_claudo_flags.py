"""`claudo_cp` no longer stands for three facts (H4: audit A45, A49, A50 and the trajectory row)."""

from factory.claudo import BuildResult
from factory.workitem import WorkItem
from tests.conftest import ScriptedExecutor
from tests.test_build_commits import log
from tests.test_claudo_build import EditingFixAgent, PlanThenBuildAgent, ScriptedClaudo, at_ship_review

CHECKPOINT = BuildResult("checkpoint", "CP-1", "T1 done\n⏸  CP-1 — waiting for x")


def test_after_a_complete_claudo_run_a_failed_gate_gets_a_targeted_fix_not_a_replay(foreman):
    foreman.executor = ScriptedExecutor([(1, "E501 line too long"), (0, "ok")])
    engine = ScriptedClaudo([BuildResult("done", log="all tasks done")])
    item = at_ship_review(foreman, engine, EditingFixAgent())
    assert len(engine.runs) == 1  # Claudo ran once; the fix was one agent's
    assert item.built_with_claudo and not item.claudo_cp
    assert log(foreman.app_dir(item))[0] == f"fix({item.slug}): targeted fix after a failed factory gate"
    assert any(a["kind"] == "fixed_after_review" for a in item.ship_acks)


def test_the_trajectory_gate_applies_after_a_complete_claudo_run(foreman):
    engine = ScriptedClaudo([BuildResult("done", log="all tasks done")])
    engine.trajectory_result = (False, "[trajectory] T1 marked done without a successful attempt")
    item = at_ship_review(foreman, engine, PlanThenBuildAgent())  # a POC: trajectory is an MVP+ gate
    result = foreman._trajectory_gate(item)
    assert not result.ok and "without a successful attempt" in result.detail


def test_a_failed_rework_after_a_rejection_is_retried_by_claudo_not_the_single_agent(foreman):
    engine = ScriptedClaudo([CHECKPOINT, BuildResult("failed", log="T1 failed"), CHECKPOINT])
    agent = PlanThenBuildAgent()
    item = at_ship_review(foreman, engine, agent)
    item = foreman.reject(item, "it", "drop the export feature", by="bob")
    item = foreman.run(item)  # Claudo's rework fails
    assert (item.stage, item.status) == ("build", "blocked") and item.claudo_rework_pending
    builds_before = [p for p, _ in agent.prompts if "implementer of" in p]
    item = foreman.run(item)  # a human re-runs: Claudo again, the rejection is not written twice
    assert len(engine.runs) == 3 and len(engine.rejected) == 1
    assert [p for p, _ in agent.prompts if "implementer of" in p] == builds_before
    assert (item.stage, item.claudo_cp, item.claudo_rework_pending) == ("ship_review", "CP-1", False)


def test_a_rejection_after_a_failed_final_run_reopens_the_checkpoint(foreman):
    engine = ScriptedClaudo([CHECKPOINT, BuildResult("failed", log="final run broke"), CHECKPOINT])
    item = at_ship_review(foreman, engine, PlanThenBuildAgent())
    item = foreman.approve(item, "it", by="bob")  # the final run fails after the token was used
    assert (item.stage, item.status) == ("ship_review", "blocked")
    item = foreman.reject(item, "it", "redo T1", by="bob")
    foreman.run(item)
    assert engine.reopened == ["CP-1"] and engine.rejected[-1][0] == "CP-1"


def test_an_item_saved_before_the_flags_existed_keeps_its_meaning():
    base = {"slug": "x", "title": "X", "idea": "i", "maturity": "mvp"}
    assert WorkItem.from_dict({**base, "claudo_cp": "CP-1"}).built_with_claudo is True
    assert WorkItem.from_dict(base).built_with_claudo is False
    assert (
        WorkItem.from_dict({**base, "claudo_cp": "CP-1", "built_with_claudo": False}).built_with_claudo
        is False
    )


def test_a_fix_after_claudo_on_a_change_is_flagged_for_it_too(foreman):
    from tests.test_change_flow import ChangeAgent, shipped_app

    app = shipped_app(foreman, "mvp")
    foreman.runner, foreman.engine = ChangeAgent(), ScriptedClaudo()
    foreman.executor = ScriptedExecutor([(1, "tests failed"), (0, "ok")])
    ch = foreman.run(foreman.intake_change(app.slug, "Add a thing", "add a thing"))
    ch = foreman.approve(foreman.approve(foreman.approve(ch, "business"), "it"), "owner")
    acks = [a for a in ch.ship_acks if a["kind"] == "fixed_after_review"]
    assert acks and "pyproject.toml" in acks[0]["detail"]
