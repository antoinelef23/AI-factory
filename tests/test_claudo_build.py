import os
import re
import subprocess

import pytest

from factory.claudo import (
    BuildResult,
    ClaudoEngine,
    EngineError,
    LintResult,
    load_or_create_secret,
    migrate_legacy_secret,
    secret_path,
    state_dir,
)
from factory.project import porcelain
from tests.conftest import VALID_SPEC, FakeAgentRunner

OK_PLAN = "---\ntype: tasks\nstatus: proposed\n---\n### T1 — Build\n- **depends_on :** []\n"


class ScriptedClaudo:
    """A Claudo stand-in: lints everything fine, replays scripted orchestrator outcomes, records calls."""

    def __init__(self, outcomes=None, sign_error=None):
        self.outcomes = list(
            outcomes or [BuildResult("checkpoint", "CP-1", "T1 done\n⏸  CP-1 — waiting for x")]
        )
        self.runs, self.signed, self.rejected, self.sign_error = [], [], [], sign_error
        self.reopened = []
        self.nonce_support = True
        self.journal_total = 0.0
        self.review = ("PASS", "work/x/.runs/CP-1-review.md")
        self.trajectory_result = (True, "[trajectory] OK")

    def lint_plan(self, slug, **kw):
        return LintResult()

    def journal_cost(self, project, slug):
        return self.journal_total

    def trajectory(self, project, slug, timeout=60):
        return self.trajectory_result

    def review_verdict(self, project, slug, cp):
        return self.review

    def run_build(self, slug, project, *, stop_at_checkpoint=True, env=None, timeout=3600):
        self.runs.append(
            {"stop": stop_at_checkpoint, "env": dict(env or {}), "timeout": timeout, "project": project}
        )
        return self.outcomes.pop(0)

    def sign_approval(self, project, slug, cp, author, secret, nonce=""):
        if self.sign_error:
            raise EngineError(self.sign_error)
        self.signed.append({"cp": cp, "author": author, "secret": secret, "project": project, "nonce": nonce})

    def supports_nonce(self):
        return self.nonce_support

    def reopen_checkpoint(self, project, slug, cp):
        self.reopened.append(cp)
        return ClaudoEngine.reopen_checkpoint(project, slug, cp)

    def reject_checkpoint(self, project, slug, cp, reason, by):
        self.rejected.append((cp, reason, by))
        return ClaudoEngine.reject_checkpoint(project, slug, cp, reason, by)


class PlanThenBuildAgent(FakeAgentRunner):
    """Answers spec/plan prompts; records the single-agent build/fix prompts."""

    def run(self, prompt, **kw):
        from factory.agents import AgentResult

        self.prompts.append((prompt, kw))
        if "spec writer" in prompt:
            return AgentResult(True, VALID_SPEC, 0.01)
        if "planner of" in prompt:
            return AgentResult(True, OK_PLAN, 0.01)
        return AgentResult(True, "fixed", 0.02)


def at_ship_review(foreman, engine, agent=None):
    foreman.runner, foreman.engine = agent or PlanThenBuildAgent(), engine
    foreman.cfg.claudo_build_from = "poc"  # these tests exercise the Claudo path at the lowest rung
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
    secret_file = secret_path(foreman.cfg.root)
    assert foreman.cfg.root not in secret_file.parents  # not in the tree the build agents work next to
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


# ------------------------------------------------------------------ maturity gating and cost


@pytest.mark.parametrize(
    ("maturity", "build_from", "claudo"),
    [
        ("poc", "mvp", False),
        ("pov", "mvp", False),
        ("mvp", "mvp", True),
        ("prod", "mvp", True),
        ("poc", "poc", True),
    ],
)
def test_claudo_builds_only_from_the_configured_maturity(foreman, maturity, build_from, claudo):
    engine = ScriptedClaudo()
    foreman.runner, foreman.engine = PlanThenBuildAgent(), engine
    foreman.cfg.claudo_build_from = build_from
    item = foreman.run(foreman.intake("X", "an api", maturity))
    item = foreman.approve(item, "business")
    if item.stage == "design_review":  # MVP and above: IT always reviews the design
        item = foreman.approve(item, "it")
    item = foreman.approve(item, "owner")
    assert bool(engine.runs) is claudo
    assert (item.claudo_cp == "CP-1") is claudo
    assert item.stage == "ship_review"  # either way the factory gates ran and IT decides


def test_default_config_builds_poc_with_the_single_agent(factory_root):
    from factory.config import load_config

    assert load_config(factory_root).claudo_build_from == "mvp"


def test_build_from_must_be_a_maturity(factory_root):
    from factory.config import ConfigError, load_config

    toml = factory_root / "factory.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace('build_from = "mvp"', 'build_from = "pilot"'),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="build_from"):
        load_config(factory_root)


def test_claudo_spend_is_added_once_from_its_journal(foreman):
    engine = ScriptedClaudo([BuildResult("checkpoint", "CP-1", "x"), BuildResult("done", log="ok")])
    engine.journal_total = 2.0
    foreman.runner, foreman.engine = PlanThenBuildAgent(), engine
    foreman.cfg.claudo_build_from = "poc"
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    item = foreman.approve(foreman.approve(item, "business"), "owner")
    agents_only = item.cost_usd - 2.0
    assert item.claudo_cost_seen == 2.0 and agents_only > 0
    engine.journal_total = 2.5  # the final run (review after approval) spent 0.5 more
    item = foreman.approve(item, "it", by="bob")
    assert item.cost_usd == pytest.approx(agents_only + 2.5) and item.claudo_cost_seen == 2.5


def test_journal_cost_sums_costs_and_tolerates_junk(tmp_path):
    runs = tmp_path / "work" / "x" / ".runs"
    runs.mkdir(parents=True)
    (runs / "journal.jsonl").write_text(
        '{"event": "task_attempt", "cost_usd": 1.25}\n{"event": "eval_fail"}\nnot json\n'
        '{"event": "review", "cost_usd": 0.75}\n{"cost_usd": null}\n[1, 2]\n',
        encoding="utf-8",
    )
    assert ClaudoEngine.journal_cost(tmp_path, "x") == pytest.approx(2.0)
    assert ClaudoEngine.journal_cost(tmp_path, "missing") == 0.0


# ------------------------------------------------------------------ replay protection (review finding C2)


def test_every_orchestrator_run_of_a_round_carries_the_same_required_nonce(foreman):
    engine = ScriptedClaudo([BuildResult("checkpoint", "CP-1", "x"), BuildResult("done", log="ok")])
    item = foreman.approve(at_ship_review(foreman, engine), "it", by="bob")
    first, second = engine.runs[0]["env"], engine.runs[1]["env"]
    assert re.fullmatch(r"[0-9a-f]{32}", first["LAB_APPROVAL_NONCE"])
    assert first["LAB_REQUIRE_NONCE"] == "1" and second["LAB_APPROVAL_NONCE"] == first["LAB_APPROVAL_NONCE"]
    assert engine.signed[0]["nonce"] == first["LAB_APPROVAL_NONCE"] == item.approval_nonce


def test_a_rejection_starts_a_new_round_with_a_new_nonce(foreman):
    engine = ScriptedClaudo(
        [BuildResult("checkpoint", "CP-1", "x"), BuildResult("checkpoint", "CP-1", "again")]
    )
    item = at_ship_review(foreman, engine)
    item = foreman.run(foreman.reject(item, "it", "too weak", by="bob"))
    assert engine.runs[1]["env"]["LAB_APPROVAL_NONCE"] != engine.runs[0]["env"]["LAB_APPROVAL_NONCE"]
    assert item.approval_nonce == engine.runs[1]["env"]["LAB_APPROVAL_NONCE"]


def test_the_bridge_passes_the_nonce_to_claudos_signer(monkeypatch, tmp_path):
    import subprocess

    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("factory.claudo.subprocess.run", fake_run)
    engine = ClaudoEngine.__new__(ClaudoEngine)
    engine.home, engine.python = tmp_path, "python"
    engine.sign_approval(tmp_path, "x", "CP-1", "bob", "secret", nonce="abc123")
    assert seen["argv"][-2:] == ["--nonce", "abc123"]
    engine.sign_approval(tmp_path, "x", "CP-1", "bob", "secret")  # no nonce: the legacy call is unchanged
    assert "--nonce" not in seen["argv"]


# ------------------------------------------------------------------ the secret is not in the factory tree


def test_a_legacy_secret_in_the_factory_tree_is_moved_not_copied(tmp_path):
    root = tmp_path / "factory"
    legacy = root / ".factory" / "approval-secret"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy-secret\n", encoding="utf-8")
    assert migrate_legacy_secret(root) is True
    assert not legacy.exists() and load_or_create_secret(secret_path(root)) == "legacy-secret"
    assert migrate_legacy_secret(root) is False  # nothing left to move


def test_migration_never_overwrites_a_secret_already_in_the_state_dir(tmp_path):
    root = tmp_path / "factory"
    legacy = root / ".factory" / "approval-secret"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("old\n", encoding="utf-8")
    target = secret_path(root)
    target.parent.mkdir(parents=True)
    target.write_text("current\n", encoding="utf-8")
    migrate_legacy_secret(root)
    assert target.read_text(encoding="utf-8").strip() == "current" and not legacy.exists()


def test_two_factories_get_two_secrets(tmp_path):
    assert secret_path(tmp_path / "a") != secret_path(tmp_path / "b")
    assert secret_path(tmp_path / "a") == secret_path(tmp_path / "a")


def test_the_state_dir_follows_the_platform_unless_overridden(monkeypatch, tmp_path):
    monkeypatch.delenv("AI_FACTORY_STATE_DIR")
    if os.name == "nt":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
        assert state_dir() == tmp_path / "local" / "ai-factory"
    else:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
        assert state_dir() == tmp_path / "xdg" / "ai-factory"
    monkeypatch.setenv("AI_FACTORY_STATE_DIR", str(tmp_path / "forced"))
    assert state_dir() == tmp_path / "forced"


def test_the_foreman_migrates_a_legacy_secret_when_it_signs(foreman):
    legacy = foreman.cfg.root / ".factory" / "approval-secret"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy-secret\n", encoding="utf-8")
    assert foreman._signing_secret() == "legacy-secret" and not legacy.exists()


class EditingFixAgent(PlanThenBuildAgent):
    """Its targeted fix really edits the app, as the live run's agent did."""

    def run(self, prompt, **kw):
        if "implementer of" in prompt:
            (kw["cwd"] / "app" / "fixed.py").write_text("FIXED = True\n", encoding="utf-8")
        return super().run(prompt, **kw)


def test_a_targeted_fix_is_committed_apart_and_shown_to_it(foreman):
    from tests.conftest import ScriptedExecutor

    foreman.executor = ScriptedExecutor([(1, "E501 line too long"), (0, "ok")])
    item = at_ship_review(foreman, ScriptedClaudo(), EditingFixAgent())
    app = foreman.app_dir(item)
    assert (item.stage, item.status) == ("ship_review", "waiting")
    assert porcelain(app) == []  # the clean-tree gate sees a committed tree
    subject = subprocess.run(
        ["git", "log", "-1", "--format=%s"], cwd=app, capture_output=True, text=True
    ).stdout.strip()
    assert subject == f"fix({item.slug}): targeted fix after a failed factory gate"
    acks = [a for a in item.ship_acks if a["kind"] == "fixed_after_review"]
    assert acks and "app/fixed.py" in acks[0]["detail"]
