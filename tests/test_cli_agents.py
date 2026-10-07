import pytest

from factory.agents import claude_argv, parse_claude_json, strip_fences
from factory.cli import main


def test_claude_argv():
    argv = claude_argv(
        "claude", model="sonnet", tools=["Read", "Glob"], max_turns=3, permission_mode="default"
    )
    assert argv[:2] == ["claude", "-p"]  # prompt goes through stdin, never argv
    assert argv[argv.index("--allowedTools") + 1] == "Read,Glob"
    assert argv[-2:] == ["--model", "sonnet"]


def test_empty_tools_means_no_tools_at_all():
    argv = claude_argv("claude", model=None, tools=[], max_turns=1, permission_mode="default")
    assert argv[-2:] == ["--tools", ""] and "--allowedTools" not in argv


def test_runner_tools_none_defaults_to_read_only_but_empty_list_is_no_tools(monkeypatch):
    import factory.agents as agents

    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        return type("P", (), {"returncode": 0, "stdout": '{"result": "ok"}', "stderr": ""})()

    monkeypatch.setattr(agents.subprocess, "run", fake_run)
    r = agents.ClaudeRunner(executable="claude")
    r.run("p", cwd=__import__("pathlib").Path("."))
    r.run("p", cwd=__import__("pathlib").Path("."), tools=[])
    assert "--allowedTools" in seen[0] and "Read,Glob,Grep" in seen[0]
    assert "--tools" in seen[1] and "--allowedTools" not in seen[1]


def test_thinking_budget_goes_to_the_environment_only_when_set(monkeypatch):
    import pathlib

    import factory.agents as agents

    envs = []

    def fake_run(argv, **kw):
        envs.append(kw.get("env"))
        return type("P", (), {"returncode": 0, "stdout": '{"result": "ok"}', "stderr": ""})()

    monkeypatch.setattr(agents.subprocess, "run", fake_run)
    r = agents.ClaudeRunner(executable="claude")
    r.run("p", cwd=pathlib.Path("."))
    r.run("p", cwd=pathlib.Path("."), thinking_tokens=0)
    r.run("p", cwd=pathlib.Path("."), thinking_tokens=3000)
    assert envs[0] is None  # untouched environment by default
    assert envs[1]["MAX_THINKING_TOKENS"] == "0"  # 0 is a real value (disable), not "unset"
    assert envs[2]["MAX_THINKING_TOKENS"] == "3000" and "PATH" in envs[2]


def test_judge_passes_thinking_budget_and_foreman_wires_config(foreman):
    from tests.conftest import JudgingAgentRunner

    runner = JudgingAgentRunner()
    foreman.runner, foreman.cfg.judge_enabled = runner, True
    foreman.cfg.judge_thinking_tokens = 1234
    seen = []
    orig = runner.run
    runner.run = lambda prompt, **kw: (seen.append(kw.get("thinking_tokens")), orig(prompt, **kw))[1]
    foreman.run(foreman.intake("X", "an api", "poc"))
    assert 1234 in seen  # the judge call carried the configured budget


def test_cli_survives_non_ascii_on_a_cp1252_console(factory_root, monkeypatch):
    """Regression: judge reports contain '≤' (not in cp1252) and the Windows console is cp1252.

    The test proves it can fail: with the fix disabled the same command raises UnicodeEncodeError."""
    import io
    import sys

    import factory.cli as cli

    monkeypatch.setenv("AI_FACTORY_ROOT", str(factory_root))

    def console():
        return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")

    title = "Frais ≤ 500"
    monkeypatch.setattr(sys, "stdout", console())
    assert main(["intake", title, "--idea", "an api"]) == 0

    monkeypatch.setattr(cli, "_utf8_console", lambda: None)  # the bug: no fix applied
    monkeypatch.setattr(sys, "stdout", console())
    with pytest.raises(UnicodeEncodeError):
        main(["show", "frais-500"])

    monkeypatch.undo()
    monkeypatch.setenv("AI_FACTORY_ROOT", str(factory_root))
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252", errors="strict"))
    assert main(["show", "frais-500"]) == 0  # with the fix: no crash
    sys.stdout.flush()
    assert b"Frais" in raw.getvalue()


def test_judge_calls_use_no_tools_and_one_turn(tmp_path):
    from factory.agents import AgentResult
    from factory.judge import judge

    class R:
        def run(self, prompt, **kw):
            self.kw = kw
            return AgentResult(True, "{}")

    r = R()
    judge(r, "spec", "a", "b", model="haiku", cwd=tmp_path)
    assert r.kw["tools"] == [] and r.kw["max_turns"] == 1


def test_parse_claude_json():
    ok = parse_claude_json(0, '{"result": "done", "total_cost_usd": 0.12}', "")
    assert ok.ok and ok.text == "done" and ok.cost_usd == 0.12
    err = parse_claude_json(0, '{"result": "", "is_error": true, "subtype": "error_max_turns"}', "")
    assert not err.ok and "error_max_turns" in err.error
    crash = parse_claude_json(1, "boom", "stack trace")
    assert not crash.ok and "stack trace" in crash.error


def test_strip_fences():
    assert strip_fences("```md\n# A\n```") == "# A\n"
    assert strip_fences("# A") == "# A\n"


def test_cli_full_offline_flow(factory_root, capsys, monkeypatch):
    monkeypatch.setenv("AI_FACTORY_ROOT", str(factory_root))
    # Avoid running uv inside tests: drop the command gate for poc.
    toml = factory_root / "factory.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace(
            'poc = ["radar", "secrets", "tests"]', 'poc = ["radar", "secrets"]'
        ),
        encoding="utf-8",
    )
    assert (
        main(["intake", "Expense tracker", "--idea", "an api to record expenses", "--maturity", "poc"]) == 0
    )
    assert main(["run", "expense-tracker"]) == 0
    assert "spec_review" in capsys.readouterr().out
    assert main(["approve", "expense-tracker", "--as", "business"]) == 0
    assert main(["approve", "expense-tracker", "--as", "owner"]) == 0
    assert main(["approve", "expense-tracker", "--as", "it"]) == 0
    assert "shipped" in capsys.readouterr().out
    assert main(["board"]) == 0
    assert "expense-tracker" in capsys.readouterr().out
    assert main(["approve", "expense-tracker", "--as", "it"]) == 2  # nothing to approve anymore


def test_cli_check_fails_on_hold(factory_root, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("AI_FACTORY_ROOT", str(factory_root))
    proj = tmp_path / "legacy"
    proj.mkdir()
    (proj / "requirements.txt").write_text("flask\nfastapi\n", encoding="utf-8")
    assert main(["check", str(proj), "--maturity", "prod"]) == 1
    out = capsys.readouterr().out
    assert "Flask" in out and "FastAPI" in out


def test_cli_radar(factory_root, capsys, monkeypatch):
    monkeypatch.setenv("AI_FACTORY_ROOT", str(factory_root))
    assert main(["radar"]) == 0
    assert "HOLD" in capsys.readouterr().out
