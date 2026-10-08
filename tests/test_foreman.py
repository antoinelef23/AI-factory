import pytest

from factory.agents import AgentResult
from factory.foreman import FactoryError
from tests.conftest import VALID_SPEC


def ship_poc(foreman, idea="A page where support agents log customer callbacks"):
    item = foreman.intake("Callback log", idea, maturity="poc")
    item = foreman.run(item)
    assert (item.stage, item.status) == ("spec_review", "waiting")
    item = foreman.approve(item, "business")
    # POC with a fully adopted stack: design review skipped, straight to plan review.
    assert (item.stage, item.status) == ("plan_review", "waiting")
    item = foreman.approve(item, "owner")
    assert (item.stage, item.status) == ("ship_review", "waiting"), item.feedback
    return foreman.approve(item, "it")


def test_poc_end_to_end_offline(foreman, executor):
    item = ship_poc(foreman)
    assert (item.stage, item.status) == ("shipped", "shipped")
    d = foreman.store.dir(item.slug)
    for name in ("idea.md", "spec.md", "design.md", "tasks.md", "gate-report.md"):
        assert (d / name).is_file(), name
    app = foreman.app_dir(item)
    assert (app / "app" / "main.py").is_file()
    assert (app / "work" / item.slug / "spec.md").is_file()  # triplet travels with the app (Claudo layout)
    assert item.slug in (app / "pyproject.toml").read_text(encoding="utf-8")  # placeholders rendered
    assert [c for c, _ in executor.calls] == ["uv run --quiet pytest -q"]  # poc gates: radar, secrets, tests
    assert [a["role"] for a in item.approvals] == ["business", "owner", "it"]


def test_wrong_role_cannot_approve(foreman):
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    with pytest.raises(FactoryError, match="must be decided by 'business'"):
        foreman.approve(item, "it")


def test_reject_sends_back_with_feedback(foreman):
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    item = foreman.reject(item, "business", "KPI missing")
    assert (item.stage, item.feedback) == ("spec", "KPI missing")
    item = foreman.run(item)
    assert item.stage == "spec_review"


def test_mvp_with_trial_tech_requires_it_design_review(foreman):
    item = foreman.run(foreman.intake("Orders", "an orders api built with Django", "mvp"))
    item = foreman.approve(item, "business")
    assert (item.stage, item.status) == ("design_review", "waiting")
    item = foreman.approve(item, "it", note="Django ok for this team")
    assert "django" in item.it_exceptions
    assert item.stage == "plan_review"


def test_business_asking_for_hold_tech_gets_note_and_alternative(foreman):
    item = foreman.run(foreman.intake("Notes", "store notes in MongoDB", "poc"))
    assert any("MongoDB" in n and "PostgreSQL" in n for n in item.notes)


def test_gate_failure_blocks_and_retries_build(foreman, executor):
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    item = foreman.approve(item, "business")
    executor.rc, executor.out = 1, "1 failed"
    item = foreman.approve(item, "owner")
    assert (item.stage, item.status) == ("build", "blocked")
    assert "tests" in item.feedback
    executor.rc = 0
    item = foreman.run(item)
    assert (item.stage, item.status) == ("ship_review", "waiting")


def test_hold_dependency_added_by_build_is_caught(foreman):
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    item = foreman.approve(item, "business")
    # Simulate an agent sneaking in a forbidden dependency before the gate runs.
    app = foreman.app_dir(item)
    real_build = foreman._do_build

    def sneaky_build(it):
        ok, detail = real_build(it)
        pyproject = app / "pyproject.toml"
        pyproject.write_text(
            pyproject.read_text(encoding="utf-8").replace('"pydantic>=2.7",', '"pydantic>=2.7", "flask",'),
            encoding="utf-8",
        )
        return ok, detail

    foreman._do_build = sneaky_build
    item = foreman.approve(item, "owner")
    assert item.status == "blocked"
    assert "Flask" in foreman.store.read(item, "gate-report.md")


def test_it_exception_and_promotion(foreman):
    item = ship_poc(foreman)
    with pytest.raises(FactoryError, match="on hold"):
        foreman.allow(item, "mongodb", "it")
    item = foreman.promote(item, "mvp", "it")
    # From MVP up, IT always reviews the design, even for a fully adopted stack.
    assert item.maturity == "mvp"
    assert (item.stage, item.status) == ("design_review", "waiting")


def reach_build(foreman, runner):
    foreman.runner = runner
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    return foreman.approve(item, "business")  # -> plan_review (waiting)


def test_agent_build_retries_after_failed_gate_then_succeeds(foreman):
    from tests.conftest import FakeAgentRunner, ScriptedExecutor

    runner = FakeAgentRunner()
    foreman.executor = ScriptedExecutor([(1, "FAILED test_x"), (0, "ok")])
    item = reach_build(foreman, runner)
    item = foreman.approve(item, "owner")  # build -> gate fails -> auto retry -> gate passes
    assert (item.stage, item.status) == ("ship_review", "waiting")
    assert item.build_attempts == 2
    build_prompts = [p for p, kw in runner.prompts if "implementer" in p]
    assert len(build_prompts) == 2
    assert "FAILED test_x" not in build_prompts[0]
    assert "FAILED test_x" in build_prompts[1]  # the failing output is fed to the second attempt
    assert [h["event"] for h in item.history].count("retry") == 1


def test_agent_build_stops_after_max_attempts(foreman):
    from tests.conftest import FakeAgentRunner, ScriptedExecutor

    foreman.executor = ScriptedExecutor([(1, "still red")])
    item = reach_build(foreman, FakeAgentRunner())
    item = foreman.approve(item, "owner")
    assert (item.stage, item.status) == ("build", "blocked")
    assert item.build_attempts == foreman.cfg.max_build_attempts == 3
    assert "still red" in item.feedback
    # A human re-run grants a fresh budget.
    foreman.executor = ScriptedExecutor([(0, "green")])
    item = foreman.run(item)
    assert (item.stage, item.status) == ("ship_review", "waiting")
    assert item.build_attempts == 4


def test_offline_build_does_not_retry(foreman, executor):
    executor.rc, executor.out = 1, "red"
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    item = foreman.approve(item, "business")
    item = foreman.approve(item, "owner")
    assert item.status == "blocked"
    assert item.build_attempts == 1
    assert len(executor.calls) == 1


def test_claude_runner_path_uses_agent_output(foreman):
    class FakeRunner:
        name = "claude"

        def __init__(self):
            self.prompts = []

        def run(self, prompt, **kw):
            self.prompts.append((prompt, kw))
            return AgentResult(True, "```markdown\n" + VALID_SPEC + "```", 0.05)

    foreman.runner = FakeRunner()
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    assert foreman.store.read(item, "spec.md") == VALID_SPEC.strip() + "\n"  # the code fence was unwrapped
    assert item.cost_usd == pytest.approx(0.05)
    assert "maturity `poc`" in foreman.runner.prompts[0][0]


# ------------------------------------------------------------ spec prompts: no invented edge cases (S-1)


def test_both_spec_prompts_forbid_edge_cases_the_idea_did_not_ask_for():
    from factory.templates import change_spec_prompt, spec_prompt
    from factory.workitem import WorkItem

    item = WorkItem(slug="x", title="x", idea="x", maturity="mvp", kind="feature", target="app")
    new, change = spec_prompt(item, ""), change_spec_prompt(item, "", "")
    for prompt in (new, change):
        assert "Every BHV must be something idea.md asks for" in prompt and "trailing slashes" in prompt
    assert "framework's DEFAULT behaviour" in change and "contradicts INV-3" in change


# ------------------------------------------------------------ the run log has its own heading (L-5)


def test_with_run_log_is_idempotent_and_ends_the_plan_with_a_table():
    from factory.templates import with_run_log

    once = with_run_log("### T1 - x\n- **depends_on :** []\n")
    assert once.rstrip().endswith("|---|---|---|---|---|") and "## Run log" in once
    assert with_run_log(once) == once


def test_every_plan_the_foreman_stores_ends_with_the_run_log(foreman):
    item = foreman.approve(foreman.run(foreman.intake("Y", "an api", "poc")), "business")
    assert "## Run log" in foreman.store.read(item, "tasks.md")
