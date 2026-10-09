"""Error paths of the small modules: every branch is exercised, none by a real network or model call."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from factory import agents, delivery, gates, identity, sandbox
from factory.agents import AgentError, ClaudeRunner, get_runner, resolve_claude
from factory.delivery import DeliveryError, GhCli
from factory.gates import command_gate, secrets_in_history, shell_executor
from factory.identity import GhIdentity, IdentityError, load_roles
from factory.project import prepare_project
from factory.sandbox import Sandbox, docker_cli


def raises(exc):
    def run(*a, **k):
        raise exc

    return run


# ------------------------------------------------------------------ agents


def test_resolve_claude_prefers_the_native_exe_behind_an_npm_shim(tmp_path, monkeypatch):
    shim = tmp_path / "claude.cmd"
    native = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    monkeypatch.setattr(agents.shutil, "which", lambda name: str(shim))
    assert resolve_claude() == str(shim)  # no native exe next to the shim: the shim itself
    native.parent.mkdir(parents=True)
    native.write_text("", encoding="utf-8")
    assert resolve_claude() == str(native)
    monkeypatch.setattr(agents.shutil, "which", lambda name: "/usr/bin/claude")
    assert resolve_claude() == "/usr/bin/claude"


def test_runners_by_name(monkeypatch):
    monkeypatch.setattr(agents, "resolve_claude", lambda: None)
    with pytest.raises(AgentError, match="not found on PATH"):
        ClaudeRunner()
    with pytest.raises(AgentError, match="not found on PATH"):
        get_runner("claude")
    monkeypatch.setattr(agents, "resolve_claude", lambda: "claude")
    assert isinstance(get_runner("claude"), ClaudeRunner) and get_runner("offline") is None
    with pytest.raises(AgentError, match="unknown runner 'gpt'"):
        get_runner("gpt")


# ------------------------------------------------------------------ delivery


def test_a_gh_call_that_hangs_is_a_delivery_error(monkeypatch):
    monkeypatch.setattr(delivery.subprocess, "run", raises(subprocess.TimeoutExpired("gh", 120)))
    with pytest.raises(DeliveryError, match="gh api user timed out"):
        GhCli().owner()


def gh_says(monkeypatch, rc, out="", err=""):
    monkeypatch.setattr(
        delivery.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=rc, stdout=out, stderr=err)
    )


def test_an_existing_repository_is_seen(monkeypatch):
    gh_says(monkeypatch, 0, '{"name": "x"}')
    assert GhCli().repo_exists("acme", "x") is True


def test_pr_checks_with_nothing_to_report(monkeypatch):
    gh_says(monkeypatch, 0)
    assert GhCli().pull_request_checks("https://github.com/a/b/pull/1") == []


# ------------------------------------------------------------------ demo


def test_the_demo_with_tests_keeps_every_gate_and_the_real_locker(factory_root):
    from factory.demo import _foreman

    f = _foreman(factory_root, with_tests=True)
    assert "tests" in f.cfg.gates_for("poc") and f.locker is not None and f.locker.__name__ != "<lambda>"


# ------------------------------------------------------------------ gates


def test_the_shell_executor_runs_and_times_out(tmp_path, monkeypatch):
    rc, out = shell_executor(f'"{sys.executable}" -c "print(42)"', tmp_path)
    assert rc == 0 and "42" in out
    monkeypatch.setattr(gates.subprocess, "run", raises(subprocess.TimeoutExpired("x", 900)))
    assert shell_executor("sleep 999", tmp_path) == (124, "timeout: sleep 999")


def test_a_gate_without_a_command_fails_and_says_where_to_set_it(tmp_path):
    result = command_gate("perf", None, tmp_path, lambda c, d: (0, ""))
    assert not result.ok and "[gates.commands]" in result.detail


def test_a_bad_history_range_is_a_clear_error(tmp_path):
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    prepare_project(tmp_path)
    with pytest.raises(ValueError, match="git log no-such-ref..HEAD failed"):
        secrets_in_history(tmp_path, "no-such-ref..HEAD")


# ------------------------------------------------------------------ identity


def test_a_hung_gh_cannot_verify_anyone(monkeypatch):
    monkeypatch.setattr(identity.subprocess, "run", raises(subprocess.TimeoutExpired("gh", 60)))
    with pytest.raises(IdentityError, match="timed out"):
        GhIdentity().current()


def test_an_unreadable_roles_file_is_an_identity_error(tmp_path):
    (tmp_path / "roles.toml").write_text("[roles", encoding="utf-8")
    with pytest.raises(IdentityError, match="roles.toml"):
        load_roles(tmp_path / "roles.toml")


# ------------------------------------------------------------------ sandbox


def test_the_docker_cli_wrapper(monkeypatch):
    monkeypatch.setattr(
        sandbox.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="29.8\n", stderr="")
    )
    assert docker_cli(["info"]) == (0, "29.8")
    monkeypatch.setattr(sandbox.subprocess, "run", raises(FileNotFoundError("docker")))
    assert docker_cli(["info"]) == (127, "docker not found")
    monkeypatch.setattr(sandbox.subprocess, "run", raises(subprocess.TimeoutExpired("docker", 30)))
    assert docker_cli(["info"]) == (124, "docker did not answer within 30s")


def test_a_missing_network_is_named(monkeypatch):
    def docker(args):
        if args[:2] == ["network", "inspect"]:
            return 1, "no such network"
        return 0, "true"

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x")
    problems = Sandbox(docker=docker).problems()
    assert "network lab-egress is missing" in problems
    assert Path(__file__).is_file()
