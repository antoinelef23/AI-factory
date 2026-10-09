"""Agent builds run sandboxed by default (ROADMAP P1-7). No Docker needed: the docker CLI is faked."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from factory import sandbox as sb
from factory.sandbox import Sandbox, SandboxedRunner
from tests.conftest import FakeAgentRunner
from tests.test_foreman import ship_poc


def ready_docker(args: list[str]) -> tuple[int, str]:
    answers = {"info": "29.8.1", "image": "sha256:abc", "network": "true", "inspect": "true"}
    return 0, answers[args[0]]


@pytest.fixture
def fake_runner():
    return FakeAgentRunner()


@pytest.fixture
def credential(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "-".join(["oat", "test"]))


def test_a_ready_sandbox_has_no_problems(credential):
    assert Sandbox(docker=ready_docker).problems() == []


def test_each_missing_piece_is_named_with_the_fix(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    def half(args):
        return {"info": (0, "29"), "image": (1, "no such image"), "network": (0, "false")}.get(
            args[0], (1, "")
        )

    problems = Sandbox(docker=half).problems()
    assert any("image lab-agent:latest is missing" in p for p in problems)
    assert any("not internal" in p for p in problems)
    assert any("egress proxy" in p for p in problems)
    assert any("setup.sh" in p for p in problems)
    assert any("claude setup-token" in p for p in problems)


def test_docker_down_is_one_clear_problem():
    assert Sandbox(docker=lambda a: (1, "cannot connect")).problems() == [
        "Docker is not running (cannot connect): start Docker Desktop"
    ]


def test_the_container_is_hardened_and_never_gets_the_secret(monkeypatch, credential):
    monkeypatch.setenv("LAB_APPROVAL_SECRET", "host-secret")
    monkeypatch.setenv("GH_TOKEN", "host-gh")
    argv = Sandbox().argv(Path("C:/apps/x"), ["sh", "-c", "uv run pytest"])
    for flag in ("--read-only", "no-new-privileges", "ALL", "lab-egress", "/home/agent:rw,exec,nosuid,nodev"):
        assert flag in argv
    assert argv[argv.index("-v") + 1].endswith("/apps/x:/workspace:rw")
    assert argv.count("-v") == 1  # only the app: no host home, no factory tree, no state directory
    assert "HTTPS_PROXY=http://lab-egress-proxy:8888" in argv and "CLAUDE_CODE_OAUTH_TOKEN=oat-test" in argv
    assert not any("host-secret" in a or "host-gh" in a for a in argv)
    assert argv[-4:] == ["lab-agent:latest", "sh", "-c", "uv run pytest"]


def test_claudo_is_told_to_contain_its_agents_and_their_code():
    env = Sandbox(network="n", proxy="http://p:1").claudo_env()
    assert env["LAB_RUNNER"] == "sandbox" and env["LAB_SANDBOX_NETWORK"] == "n"
    assert env["LAB_SANDBOX_PROXY"] == "http://p:1"


def test_the_gate_executor_runs_the_command_in_the_container(monkeypatch, tmp_path):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return SimpleNamespace(returncode=3, stdout="out", stderr="err")

    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    assert Sandbox().executor("uv run --quiet pytest -q", tmp_path) == (3, "outerr")
    assert seen["argv"][-3:] == ["sh", "-c", "uv run --quiet pytest -q"]
    assert "UV_PROJECT_ENVIRONMENT=/workspace/.venv" in seen["argv"]  # the licence check reads it afterwards


def test_the_sandboxed_runner_sends_the_prompt_on_stdin(monkeypatch, tmp_path):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, input=kw["input"])
        return SimpleNamespace(returncode=0, stdout='{"result": "built", "total_cost_usd": 0.5}', stderr="")

    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    result = SandboxedRunner(Sandbox()).run("build it", cwd=tmp_path, tools=["Edit"], model="sonnet")
    assert (result.ok, result.text, result.cost_usd) == (True, "built", 0.5)
    assert seen["input"] == "build it" and "build it" not in seen["argv"]
    assert seen["argv"][seen["argv"].index("lab-agent:latest") + 1 :][:2] == ["claude", "-p"]


# ---------------------------------------------------------------- the foreman's policy


def down(args):
    return 1, "down"


def test_an_unready_sandbox_blocks_an_mvp_build_without_spending_an_attempt(foreman, fake_runner):
    foreman.sandbox, foreman.runner = Sandbox(docker=down), fake_runner
    item = foreman.intake("X", "an api", "mvp")
    item.stage, item.status = "build", "active"
    item = foreman.run(item)
    assert (item.stage, item.status) == ("build", "blocked")
    assert "Docker is not running" in item.feedback and "--unsafe-host" in item.feedback
    assert item.build_attempts == 0


def test_an_unready_sandbox_lets_a_poc_build_on_the_host_with_a_note(foreman, fake_runner):
    foreman.sandbox = Sandbox(docker=down)
    item = foreman.approve(foreman.run(foreman.intake("X", "an api", "poc")), "business")
    foreman.runner = fake_runner  # an agent writes the code from here on
    item = foreman.approve(item, "owner")
    assert item.build_where == "host" and item.build_attempts == 1
    assert any("required from MVP up" in n for n in item.notes)
    assert not any(a["kind"] == "unsafe_host" for a in item.ship_acks)  # D5: optional below MVP


def test_a_ready_sandbox_contains_the_agent_the_gates_and_claudo(foreman, fake_runner, credential):
    foreman.sandbox, foreman.runner = Sandbox(docker=ready_docker), fake_runner
    item = foreman.intake("X", "an api", "mvp")
    assert foreman._containment(item) == "" and item.build_where == "sandbox"
    assert isinstance(foreman._builder(item), SandboxedRunner)
    assert foreman._gate_executor(item) == foreman.sandbox.executor
    assert foreman._claudo_env(item)["LAB_RUNNER"] == "sandbox"
    assert "Agent code ran in: **sandbox" in foreman._pr_body(item)


def test_offline_nothing_needs_the_sandbox(foreman):
    foreman.sandbox = Sandbox(docker=down)  # no agent writes code: Docker is irrelevant
    item = ship_poc(foreman)
    assert item.status == "shipped" and item.build_where == ""
    assert foreman._gate_executor(item) == foreman.executor


def test_unsafe_host_is_recorded_for_it_to_acknowledge(foreman, fake_runner):
    foreman.runner, foreman.unsafe_host = fake_runner, True
    item = foreman.intake("X", "an api", "mvp")
    assert foreman._containment(item) == "" and item.build_where == "host"
    foreman._do_build(item)
    assert any(a["kind"] == "unsafe_host" for a in item.ship_acks)
