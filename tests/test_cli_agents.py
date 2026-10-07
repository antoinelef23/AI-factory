from factory.agents import claude_argv, parse_claude_json, strip_fences
from factory.cli import main


def test_claude_argv():
    argv = claude_argv(
        "claude", model="sonnet", tools=["Read", "Glob"], max_turns=3, permission_mode="default"
    )
    assert argv[:2] == ["claude", "-p"]  # prompt goes through stdin, never argv
    assert argv[argv.index("--allowedTools") + 1] == "Read,Glob"
    assert argv[-2:] == ["--model", "sonnet"]


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
