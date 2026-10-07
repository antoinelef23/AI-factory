import pytest

from tests.conftest import JudgingAgentRunner


def judged_foreman(foreman, runner, enabled=True):
    foreman.runner = runner
    foreman.cfg.judge_enabled = enabled
    foreman.cfg.models["judge"] = "haiku"
    return foreman


def judge_calls(runner):
    return [(p, kw) for p, kw in runner.prompts if "independent reviewer" in p]


def test_judge_is_off_by_default(foreman):
    runner = JudgingAgentRunner()
    f = judged_foreman(foreman, runner, enabled=False)
    item = f.run(f.intake("X", "an api", "poc"))
    assert judge_calls(runner) == [] and item.judgements == {}


def test_offline_never_judges_even_when_enabled(foreman):
    foreman.cfg.judge_enabled = True
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    assert item.judgements == {} and foreman.runner is None


def test_spec_plan_and_build_are_judged_and_recorded(foreman):
    runner = JudgingAgentRunner(score=5)
    f = judged_foreman(foreman, runner)
    item = f.run(f.intake("X", "an api", "poc"))
    assert item.judgements["spec"]["verdict"] == "pass"
    assert (f.store.dir(item.slug) / "judge-spec.md").is_file()
    item = f.approve(item, "business")  # plan written + judged
    item = f.approve(item, "owner")  # build, gates, build judged
    assert set(item.judgements) == {"spec", "plan", "build"}
    assert [h["event"] for h in item.history].count("judged") == 3
    assert item.cost_usd == pytest.approx(3 * 0.01 + 3 * 0.002)  # spec/plan/build agents + 3 judge calls
    assert all(kw["model"] == "haiku" for _, kw in judge_calls(runner))  # judge model != generator model


def test_judge_never_blocks_even_on_a_failing_verdict(foreman):
    runner = JudgingAgentRunner(score=1)
    f = judged_foreman(foreman, runner)
    item = f.run(f.intake("X", "an api", "poc"))
    assert item.judgements["spec"]["verdict"] == "fail"
    assert (item.stage, item.status) == ("spec_review", "waiting")  # advice only: the human decides
    item = f.approve(item, "business")
    item = f.approve(item, "owner")
    assert (item.stage, item.status) == (
        "ship_review",
        "waiting",
    )  # even a failing build verdict does not block


def test_build_judge_sees_code_and_spec(foreman):
    runner = JudgingAgentRunner()
    f = judged_foreman(foreman, runner)
    item = f.approve(f.run(f.intake("X", "an api", "poc")), "business")
    f.approve(item, "owner")
    prompt = next(p for p, _ in judge_calls(runner) if "reviewer of a build" in p)
    assert "### app/main.py" in prompt and "### tests/test_health.py" in prompt
    assert "<spec>" in prompt


def test_cli_judge_command(factory_root, capsys, monkeypatch):
    import factory.cli as cli

    monkeypatch.setenv("AI_FACTORY_ROOT", str(factory_root))
    runner = JudgingAgentRunner()
    monkeypatch.setattr(cli, "get_runner", lambda name: runner if name == "claude" else None)
    assert cli.main(["intake", "X", "--idea", "an api"]) == 0
    assert cli.main(["run", "x"]) == 0  # offline: spec exists, nothing judged
    capsys.readouterr()
    assert cli.main(["judge", "x", "--kind", "spec"]) == 0
    assert "Judge report: spec (pass" in capsys.readouterr().out
    assert cli.main(["show", "x"]) == 0
    assert "judge spec: pass" in capsys.readouterr().out
    assert cli.main(["judge", "x", "--kind", "build"]) == 2  # nothing built yet
