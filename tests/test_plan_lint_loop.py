import pytest

from factory.agents import AgentResult
from factory.claudo import LintResult
from factory.templates import with_run_log
from tests.conftest import VALID_SPEC

GOOD = "---\ntype: tasks\nstatus: proposed\n---\n### T1 — good plan\n- **depends_on :** []\n"
BAD = "# Tasks: not the Claudo format\n### T1: bad plan\n"


class Scripted:
    """An agent that answers spec first, then each plan attempt from `plans` in order."""

    name = "claude"

    def __init__(self, plans):
        self.plans, self.prompts = list(plans), []

    def run(self, prompt, **kw):
        self.prompts.append(prompt)
        if "spec writer" in prompt:
            return AgentResult(True, VALID_SPEC, 0.01)
        if "implementer" in prompt:  # the build stage after the owner approves
            return AgentResult(True, "built", 0.01)
        return AgentResult(True, self.plans.pop(0), 0.01)


class FakeEngine:
    """Claudo stand-in: a plan is valid iff it contains the Claudo task header."""

    def __init__(self):
        self.calls = []

    def lint_plan(self, slug, *, spec, design, tasks, timeout=60):
        self.calls.append(tasks)
        if "### T1 — " in tasks:
            return LintResult(warnings=["T1: no executable verify"])
        return LintResult(errors=["no task recognized — check the format `### T1 — title`"])

    def review_verdict(self, project, slug, cp):
        return None

    def run_build(self, slug, project, **kw):  # only reached once the owner approved a plan
        from factory.claudo import BuildResult

        return BuildResult("checkpoint", "CP-1", "built")


def to_plan(foreman, runner, engine):
    foreman.runner, foreman.engine = runner, engine
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    return foreman.approve(item, "business")  # design (compiled) then plan (agent)


def test_bad_plan_is_reprompted_with_the_lint_errors_then_accepted(foreman):
    runner, engine = Scripted([BAD, GOOD]), FakeEngine()
    item = to_plan(foreman, runner, engine)
    assert (item.stage, item.status) == ("plan_review", "waiting")
    assert foreman.store.read(item, "tasks.md") == with_run_log(GOOD)
    plan_prompts = [p for p in runner.prompts if "planner of" in p]
    assert len(plan_prompts) == 2
    assert "FAILED Claudo's plan-lint" not in plan_prompts[0]
    assert "no task recognized" in plan_prompts[1] and BAD.strip() in plan_prompts[1]  # errors + prior output
    assert [h["event"] for h in item.history].count("lint") == 1
    assert "(warning) T1: no executable verify" in foreman.store.read(item, "plan-lint.md")


def test_first_try_good_plan_makes_no_retry(foreman):
    runner = Scripted([GOOD])
    item = to_plan(foreman, runner, FakeEngine())
    assert len([p for p in runner.prompts if "planner of" in p]) == 1
    assert "lint" not in [h["event"] for h in item.history]


def test_plan_prompt_carries_the_format_and_the_real_spec_ids(foreman):
    runner = Scripted([GOOD])
    to_plan(foreman, runner, FakeEngine())
    prompt = next(p for p in runner.prompts if "planner of" in p)
    assert "EXACT FORMAT" in prompt and "### T1 — Short title" in prompt and "EM DASH" in prompt
    from factory.templates import SPEC_ID

    ids = ", ".join(sorted(set(SPEC_ID.findall(VALID_SPEC))))
    assert f"Spec IDs you may reference in `implements`: {ids}." in prompt and "BHV-2" in ids


def test_retries_are_bounded_then_the_item_blocks_with_the_errors(foreman):
    foreman.cfg.plan_lint_retries = 2
    runner, engine = Scripted([BAD, BAD, BAD]), FakeEngine()
    item = to_plan(foreman, runner, engine)
    assert (item.stage, item.status) == ("plan", "blocked")
    assert len(engine.calls) == 3 and runner.plans == []  # first try + 2 retries, no more
    assert "rejected by Claudo plan-lint (3 attempt(s))" in item.feedback
    assert "no task recognized" in item.feedback
    assert foreman.store.read(item, "tasks.md") == BAD  # kept for the human to inspect
    assert not (foreman.store.dir(item.slug) / "judge-plan.md").exists()


def test_zero_retries_means_one_attempt(foreman):
    foreman.cfg.plan_lint_retries = 0
    runner = Scripted([BAD, GOOD])
    item = to_plan(foreman, runner, FakeEngine())
    assert item.status == "blocked" and len(runner.plans) == 1


def test_an_offline_template_that_fails_lint_is_a_bug_and_blocks_without_retry(foreman):
    class AlwaysRejects(FakeEngine):
        def lint_plan(self, slug, **kw):
            self.calls.append(kw["tasks"])
            return LintResult(errors=["T1: files_touched missing"])

    engine = AlwaysRejects()
    foreman.engine = engine
    item = foreman.approve(foreman.run(foreman.intake("X", "an api", "poc")), "business")
    assert item.status == "blocked" and "offline template" in item.feedback
    assert len(engine.calls) == 1  # nothing to re-prompt: no agent


def test_without_an_engine_nothing_is_linted(foreman):
    runner = Scripted([BAD])
    item = to_plan(foreman, runner, None)
    assert item.status == "waiting" and foreman.store.read(item, "tasks.md") == with_run_log(BAD)
    assert not (foreman.store.dir(item.slug) / "plan-lint.md").exists()


def test_owner_approval_stamps_the_plan_approved_for_claudo(foreman):
    item = to_plan(foreman, Scripted([GOOD]), FakeEngine())
    assert "status: proposed" in foreman.store.read(item, "tasks.md")
    foreman.approve(item, "owner", by="antoine")
    text = foreman.store.read(item, "tasks.md")
    assert "status: approved" in text and "approved_by: antoine" in text and "status: proposed" not in text


def test_approving_twice_does_not_restamp(foreman):
    item = to_plan(foreman, Scripted([GOOD]), FakeEngine())
    foreman._mark_plan_approved(item, "a")
    once = foreman.store.read(item, "tasks.md")
    foreman._mark_plan_approved(item, "b")
    assert foreman.store.read(item, "tasks.md") == once


@pytest.mark.parametrize("retries", [-5, "x"])
def test_config_rejects_nonsense_retry_counts(factory_root, retries):
    from factory.config import load_config

    toml = factory_root / "factory.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace("plan_lint_retries = 2", f"plan_lint_retries = {retries!r}"),
        encoding="utf-8",
    )
    if isinstance(retries, str):
        with pytest.raises(ValueError):
            load_config(factory_root)
    else:
        assert load_config(factory_root).plan_lint_retries == 0  # clamped, never negative


# ------------------------------------------------------------------ the planner is told the app exists


def test_plan_prompt_says_the_app_is_already_scaffolded_and_lists_its_files(foreman):
    """Regression (live run, $3.29 / 11 min): T1 was 'scaffold the app', redone over a working app."""
    runner = Scripted([GOOD])
    to_plan(foreman, runner, FakeEngine())
    prompt = next(p for p in runner.prompts if "planner of" in p)
    assert "ALREADY SCAFFOLDED" in prompt and "Never plan a scaffolding/setup task" in prompt
    assert "app/main.py" in prompt and "tests/test_health.py" in prompt and "justfile" in prompt
    assert "test_eval_<n>_" in prompt and "@pytest.mark.eval" in prompt  # the merge gate runs only evals


def test_offline_plan_has_no_scaffold_task_and_keeps_the_eval_convention(foreman):
    item = foreman.approve(foreman.run(foreman.intake("X", "an api", "poc")), "business")
    tasks = foreman.store.read(item, "tasks.md")
    headers = [ln for ln in tasks.splitlines() if ln.startswith("### ")]
    assert [h.split(" — ")[0] for h in headers] == ["### T1", "### T2", "### CP-1"]
    assert not any("caffold" in h for h in headers)  # no scaffolding task: the app already exists
    assert "already scaffolded" in tasks and "@pytest.mark.eval" in tasks


def test_golden_health_test_is_a_real_eval():
    from pathlib import Path

    text = (
        Path(__file__).resolve().parents[1] / "golden_paths/python-fastapi/tests/test_health.py"
    ).read_text(encoding="utf-8")
    assert "@pytest.mark.eval" in text and "def test_eval_1_health" in text
