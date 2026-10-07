import re
import subprocess

import pytest

from factory.claudo import BuildResult, ClaudoEngine, EngineError, LintResult, load_or_create_secret
from tests.conftest import FakeAgentRunner

OK_PLAN = "---\ntype: tasks\nstatus: proposed\n---\n### T1 — Build\n- **depends_on :** []\n"


class ScriptedClaudo:
    """A Claudo stand-in: lints everything fine, replays scripted orchestrator outcomes, records calls."""

    def __init__(self, outcomes=None, sign_error=None):
        self.outcomes = list(
            outcomes or [BuildResult("checkpoint", "CP-1", "T1 done\n⏸  CP-1 — waiting for x")]
        )
        self.runs, self.signed, self.rejected, self.sign_error = [], [], [], sign_error

    def lint_plan(self, slug, **kw):
        return LintResult()

    def run_build(self, slug, project, *, stop_at_checkpoint=True, env=None, timeout=3600):
        self.runs.append(
            {"stop": stop_at_checkpoint, "env": dict(env or {}), "timeout": timeout, "project": project}
        )
        return self.outcomes.pop(0)

    def sign_approval(self, project, slug, cp, author, secret):
        if self.sign_error:
            raise EngineError(self.sign_error)
        self.signed.append({"cp": cp, "author": author, "secret": secret, "project": project})

    def reject_checkpoint(self, project, slug, cp, reason, by):
        self.rejected.append((cp, reason, by))
        return ClaudoEngine.reject_checkpoint(project, slug, cp, reason, by)


class PlanThenBuildAgent(FakeAgentRunner):
    """Answers spec/plan prompts; records the single-agent build/fix prompts."""

    def run(self, prompt, **kw):
        from factory.agents import AgentResult

        self.prompts.append((prompt, kw))
        if "spec writer" in prompt:
            return AgentResult(True, "# Spec\n- **INV-1** — x\n", 0.01)
        if "planner of" in prompt:
            return AgentResult(True, OK_PLAN, 0.01)
        return AgentResult(True, "fixed", 0.02)


def at_ship_review(foreman, engine, agent=None):
    foreman.runner, foreman.engine = agent or PlanThenBuildAgent(), engine
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    item = foreman.approve(item, "business")
    return foreman.approve(item, "owner", by="antoine")


def test_build_runs_claudo_stops_at_its_checkpoint_then_the_factory_gates_run(foreman, executor):
    engine = ScriptedClaudo()
    item = at_ship_review(foreman, engine)
    assert (item.stage, item.status) == ("ship_review", "waiting")  # factory gates passed after Claudo
    assert item.claudo_cp == "CP-1"
    [run] = engine.runs
    assert run["stop"] is True and run["project"] == foreman.app_dir(item) and run["timeout"] == 3600
    assert "⏸  CP-1" in foreman.store.read(item, "claudo-build.log")
    assert [c for c, _ in executor.calls] == ["uv run --quiet pytest -q"]  # factory gates ran on the result


def test_the_app_is_a_git_project_with_the_approved_plan_and_the_evals_recipe(foreman):
    item = at_ship_review(foreman, ScriptedClaudo())
    app = foreman.app_dir(item)
    assert (app / ".git").is_dir() and "evals:" in (app / "justfile").read_text(encoding="utf-8")
    tasks = (app / "work" / item.slug / "tasks.md").read_text(encoding="utf-8")
    assert "status: approved" in tasks and "approved_by: antoine" in tasks  # what Claudo requires to run
    log = subprocess.run(["git", "log", "--format=%an"], cwd=app, capture_output=True, text=True).stdout
    assert log.strip() == "AI Factory"


def test_the_orchestrator_gets_the_signing_secret_and_the_budget(foreman):
    foreman.cfg.claudo_budget_usd = 7.5
    engine = ScriptedClaudo()
    at_ship_review(foreman, engine)
    env = engine.runs[0]["env"]
    assert re.fullmatch(r"[0-9a-f]{64}", env["LAB_APPROVAL_SECRET"]) and env["LAB_BUDGET_USD"] == "7.5"


def test_the_secret_is_stable_private_and_outside_the_app(foreman, tmp_path):
    engine = ScriptedClaudo()
    item = at_ship_review(foreman, engine)
    secret_file = foreman.cfg.root / ".factory" / "approval-secret"
    secret = secret_file.read_text(encoding="utf-8").strip()
    assert secret == engine.runs[0]["env"]["LAB_APPROVAL_SECRET"]
    assert foreman.app_dir(item) not in secret_file.parents  # not reachable from the app's folder
    assert load_or_create_secret(secret_file) == secret  # stable across calls and runs


def test_it_approval_signs_with_the_approvers_name_and_lets_claudo_finish(foreman):
    engine = ScriptedClaudo([BuildResult("checkpoint", "CP-1", "x"), BuildResult("done", log="all green")])
    item = at_ship_review(foreman, engine)
    item = foreman.approve(item, "it", by="bob")
    assert (item.stage, item.status) == ("shipped", "shipped") and item.claudo_cp == ""
    [sig] = engine.signed
    assert sig["cp"] == "CP-1" and sig["author"] == "bob"
    assert sig["secret"] == engine.runs[0]["env"]["LAB_APPROVAL_SECRET"]  # same secret Claudo verifies with
    assert engine.runs[1]["stop"] is False  # this run is allowed to pass the checkpoint
    assert "all green" in foreman.store.read(item, "claudo-final.log")


def test_a_failed_signature_blocks_the_ship_and_nothing_ships(foreman):
    engine = ScriptedClaudo(sign_error="signing CP-1 failed: boom")
    item = foreman.approve(at_ship_review(foreman, engine), "it", by="bob")
    assert (item.stage, item.status) == ("ship_review", "blocked") and "boom" in item.feedback
    assert item.approvals[-1]["role"] != "it"  # the approval was NOT recorded


def test_claudo_not_completing_after_the_approval_blocks_the_ship(foreman):
    engine = ScriptedClaudo([BuildResult("checkpoint", "CP-1", "x"), BuildResult("failed", log="T2 failed")])
    item = foreman.approve(at_ship_review(foreman, engine), "it", by="bob")
    assert item.status == "blocked" and "T2 failed" in item.feedback and item.stage == "ship_review"


def test_a_claudo_build_failure_blocks_with_its_log(foreman):
    engine = ScriptedClaudo([BuildResult("failed", log="❌ T3 failed: tests red")])
    item = at_ship_review(foreman, engine)
    assert (item.stage, item.status) == ("build", "blocked")
    assert "Claudo build failed" in item.feedback and "T3 failed" in item.feedback


def test_it_rejection_reopens_the_work_in_claudo_with_the_reason(foreman):
    engine = ScriptedClaudo(
        [BuildResult("checkpoint", "CP-1", "x"), BuildResult("checkpoint", "CP-1", "again")]
    )
    item = at_ship_review(foreman, engine)
    item = foreman.reject(item, "it", "validation of amounts is too weak", by="bob")
    assert item.claudo_rejection["cp"] == "CP-1" and item.stage == "build"
    item = foreman.run(item)  # rebuild: the rejection file goes in, Claudo reopens, pauses again
    app = foreman.app_dir(item)
    rejected = (app / "work" / item.slug / ".approvals" / "CP-1.rejected").read_text(encoding="utf-8")
    assert rejected == "reason=validation of amounts is too weak\nby=bob\n"
    assert engine.rejected == [("CP-1", "validation of amounts is too weak", "bob")]
    assert (item.stage, item.status) == ("ship_review", "waiting") and item.claudo_rejection == {}
    assert len(engine.runs) == 2


def test_a_factory_gate_failure_after_claudo_gets_a_targeted_fix_not_a_replay(foreman, executor):
    from tests.conftest import ScriptedExecutor

    agent, engine = PlanThenBuildAgent(), ScriptedClaudo()
    foreman.executor = ScriptedExecutor([(1, "1 failed: test_x"), (0, "ok")])
    item = at_ship_review(foreman, engine, agent)  # Claudo ran once; gate failed; one targeted fix; gate ok
    assert (item.stage, item.status) == ("ship_review", "waiting")
    assert len(engine.runs) == 1  # the plan was NOT replayed
    fix = [p for p, _ in agent.prompts if "implementer of" in p]
    assert len(fix) == 1 and "1 failed: test_x" in fix[0]  # the single agent got the gate report


def test_offline_never_touches_the_engine(foreman):
    engine = ScriptedClaudo()
    foreman.engine = engine
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    item = foreman.approve(foreman.approve(item, "business"), "owner")
    assert item.stage == "ship_review" and engine.runs == [] and item.claudo_cp == ""


def test_rejection_of_a_non_claudo_build_is_unchanged(foreman):
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    item = foreman.approve(foreman.approve(item, "business"), "owner")
    item = foreman.reject(item, "it", "needs work")
    assert item.claudo_rejection == {} and item.stage == "build"


def test_secret_file_is_created_with_restrictive_intent(tmp_path):
    f = tmp_path / ".factory" / "s"
    value = load_or_create_secret(f)
    assert len(value) == 64 and f.read_text(encoding="utf-8").strip() == value
    f.write_text("custom-secret\n", encoding="utf-8")
    assert load_or_create_secret(f) == "custom-secret"  # an existing secret is never overwritten
    f.write_text("\n", encoding="utf-8")
    assert len(load_or_create_secret(f)) == 64  # an empty file is not a secret


@pytest.mark.parametrize("reason", ["line one\nline two", "  spaced   out  "])
def test_reject_checkpoint_writes_a_single_line_reason(tmp_path, reason):
    path = ClaudoEngine.reject_checkpoint(tmp_path, "x", "CP-1", reason, "bob")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("reason=") and lines[1] == "by=bob" and len(lines) == 2
    assert "\n" not in lines[0]
