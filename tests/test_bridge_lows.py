"""Agent, engine and host bridges fail clearly (audit A84-A86, A93-A95, A134, A136)."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from factory import agents, claudo, delivery
from factory.agents import ClaudeRunner, parse_claude_json
from factory.claudo import ClaudoEngine, EngineError
from factory.delivery import DeliveryError, GhCli
from factory.foreman import FactoryError
from tests.test_claudo_bridge import fake_claudo
from tests.test_claudo_build import ScriptedClaudo, at_ship_review


def test_exit_zero_without_json_is_a_failed_agent_run():
    for stdout in ("", "Usage: claude [options]"):
        result = parse_claude_json(0, stdout, "")
        assert not result.ok and "printed no JSON result" in result.error
    assert parse_claude_json(2, "", "").error == "exit code 2"


def test_a_timed_out_agent_says_its_cost_is_unknown(monkeypatch, tmp_path):
    def hang(*a, **k):
        raise subprocess.TimeoutExpired("claude", 5)

    monkeypatch.setattr(agents.subprocess, "run", hang)
    result = ClaudeRunner(executable="claude", timeout=5).run("p", cwd=tmp_path)
    assert not result.ok and "cost is unknown" in result.error


def test_a_hung_trajectory_guard_fails_the_gate(monkeypatch, tmp_path):
    engine = ClaudoEngine(fake_claudo(tmp_path / "c"))

    def hang(*a, **k):
        raise subprocess.TimeoutExpired("guard", 60)

    monkeypatch.setattr(claudo.subprocess, "run", hang)
    assert engine.trajectory(tmp_path, "x") == (False, "the trajectory guard did not finish within 60s")
    with pytest.raises(EngineError, match="did not finish within 60s"):
        engine.sign_approval(tmp_path, "x", "CP-1", "bob", "secret")


def gh_answering(monkeypatch, stdout):
    monkeypatch.setattr(
        delivery.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=stdout, stderr="")
    )


@pytest.mark.parametrize("stdout", ["A new release of gh is available", "{}", "[]"])
def test_an_unreadable_pull_request_state_is_a_delivery_error(monkeypatch, stdout):
    gh_answering(monkeypatch, stdout)
    with pytest.raises(DeliveryError, match="unreadable answer"):
        GhCli().pull_request_state("https://github.com/a/b/pull/1")


def test_the_inbox_says_when_it_hit_its_issue_limit(foreman, monkeypatch):
    from tests.test_inbox import IssueHost, issue

    monkeypatch.setattr("factory.foreman.ISSUE_LIMIT", 2)
    foreman.host = IssueHost([issue(1, "First"), issue(2, "Second")])
    foreman.cfg.intake_repo, foreman.cfg.intake_label = "acme/ideas", "factory"
    _, skipped = foreman.inbox()
    assert any("2 or more open issues" in s for s in skipped)


def test_the_cli_reads_the_issue_list_limit(monkeypatch):
    seen = {}

    def run(argv, **kw):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=json.dumps([]), stderr="")

    monkeypatch.setattr(delivery.subprocess, "run", run)
    GhCli().list_issues("acme/ideas", "factory")
    assert seen["argv"][seen["argv"].index("--limit") + 1] == str(delivery.ISSUE_LIMIT)


def test_the_demo_follows_the_radar_and_golden_paths_factory_toml_names(factory_root):
    from factory.demo import run_demo

    (factory_root / "it").mkdir()
    (factory_root / "radar.toml").rename(factory_root / "it" / "radar.toml")
    (factory_root / "golden_paths").rename(factory_root / "templates")
    toml = factory_root / "factory.toml"
    text = toml.read_text(encoding="utf-8")
    text = text.replace('radar = "radar.toml"', 'radar = "it/radar.toml"', 1)
    text = text.replace('golden_paths_dir = "golden_paths"', 'golden_paths_dir = "templates"', 1)
    toml.write_text(text, encoding="utf-8")
    lines = []
    run_demo(factory_root, say=lines.append)
    assert any("DEMO OK" in line for line in lines)


def test_approving_a_claudo_checkpoint_without_claudo_says_what_to_do(foreman):
    item = at_ship_review(foreman, ScriptedClaudo())
    foreman.engine = None
    with pytest.raises(FactoryError, match="paused at Claudo's checkpoint CP-1"):
        foreman.approve(item, "it", by="bob")


def test_a_gate_failure_is_logged_at_the_gate_then_goes_back_to_build(foreman, executor):
    from tests.conftest import ScriptedExecutor

    foreman.executor = ScriptedExecutor([(1, "FAILED test_x")])
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    item = foreman.approve(foreman.approve(item, "business"), "owner")  # offline: no retry
    failed = [h for h in item.history if h["event"] == "failed"]
    assert failed[-1]["stage"] == "gate" and (item.stage, item.status) == ("build", "blocked")
