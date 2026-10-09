"""The judge as a panel: several runs per artifact, median per criterion (one run is noisy)."""

import json

import pytest

from factory.agents import AgentResult
from factory.config import load_config
from factory.judge import RUBRICS, combine, evaluate, judge_panel

ARTIFACT = (
    "# Spec\n\n- INV-1: every stored record MUST have a positive amount in EUR.\n"
    "- BHV-1: Given a valid request, When it is submitted, Then the response is 201.\n"
)
QUOTE = "every stored record MUST have a positive amount in EUR."


def answer(scores, summary="s", quote=QUOTE):
    """A judge answer giving `scores[i]` to the i-th spec criterion (missing ones are not scored)."""
    crit = [
        {"id": cid, "score": s, "evidence": f"{cid} e", "quote": quote}
        for (cid, _), s in zip(RUBRICS["spec"], scores, strict=False)
    ]
    return json.dumps({"criteria": crit, "summary": summary})


class ScriptedJudge:
    """Returns one scripted answer per call, in order; counts the calls."""

    def __init__(self, answers, cost=0.03):
        self.answers, self.cost, self.calls = list(answers), cost, 0

    def run(self, prompt, **kw):
        self.calls += 1
        return AgentResult(True, self.answers.pop(0), self.cost)


def report(scores, quote=QUOTE):
    return evaluate("spec", answer(scores, quote=quote), ARTIFACT)


# ------------------------------------------------------------------ combining runs


def test_one_run_is_returned_as_is():
    r = report([5, 5, 5, 5, 5, 5])
    assert combine("spec", [r]) is r


def test_each_criterion_takes_the_median_and_the_verdict_follows_the_medians():
    runs = [report([5, 5, 5, 5, 2, 5]), report([5, 4, 5, 5, 4, 5]), report([4, 5, 5, 5, 4, 5])]
    assert [r.verdict for r in runs] == ["fail", "pass", "pass"]
    panel = combine("spec", runs)
    scores = {c.id: c.score for c in panel.criteria}
    assert scores["examples"] == 4  # median of 2, 4, 4: one outlier run no longer fails the artifact
    assert panel.verdict == "pass" and panel.votes == ["fail", "pass", "pass"]
    assert "(scores: 2, 4, 4)" in next(c.evidence for c in panel.criteria if c.id == "examples")


def test_a_real_problem_seen_by_most_runs_still_fails():
    runs = [report([5, 5, 5, 5, 2, 5]), report([5, 5, 5, 5, 1, 5]), report([5, 5, 5, 5, 4, 5])]
    assert combine("spec", runs).verdict == "fail"  # median 2


def test_unreliable_runs_do_not_vote_and_a_majority_of_them_makes_the_panel_unreliable():
    good = report([5, 5, 5, 5, 4, 5])
    bad = report([5, 5, 5, 5, 5, 5], quote="text that is not in the artifact at all, invented")
    assert bad.verdict == "unreliable"
    assert combine("spec", [good, bad, good]).verdict == "pass"
    panel = combine("spec", [good, bad, bad])
    assert panel.verdict == "unreliable" and "2 of 3 runs were unreliable" in panel.summary


def test_the_panel_sums_the_cost_and_says_how_it_voted():
    runs = [report([5, 5, 5, 5, 5, 5]) for _ in range(3)]
    for r in runs:
        r.cost_usd = 0.04
    panel = combine("spec", runs)
    assert panel.cost_usd == pytest.approx(0.12)
    assert "Panel of 3 runs" in panel.markdown() and "[votes: pass, pass, pass]" in panel.short()


# ------------------------------------------------------------------ running the panel


def test_judge_panel_runs_the_judge_once_per_vote():
    runner = ScriptedJudge(
        [answer([5, 5, 5, 5, 2, 5]), answer([5, 5, 5, 5, 4, 5]), answer([5, 5, 5, 5, 4, 5])]
    )
    panel = judge_panel(runner, "spec", ARTIFACT, "an idea", votes=3, model="sonnet", cwd=".")
    assert runner.calls == 3 and panel.verdict == "pass" and panel.cost_usd == pytest.approx(0.09)


def test_votes_below_one_still_run_the_judge_once():
    runner = ScriptedJudge([answer([5, 5, 5, 5, 5, 5])])
    assert judge_panel(runner, "spec", ARTIFACT, "i", votes=0, model=None, cwd=".").verdict == "pass"
    assert runner.calls == 1


def test_the_foreman_judges_with_the_configured_number_of_votes(foreman):
    from tests.conftest import JudgingAgentRunner

    runner = JudgingAgentRunner(score=5)
    foreman.runner, foreman.cfg.judge_enabled, foreman.cfg.judge_votes = runner, True, 3
    item = foreman.intake("X", "an api", "poc")
    foreman.store.write(item, "spec.md", ARTIFACT)
    rep = foreman.judge_artifact(item, "spec")
    judge_calls = [p for p, _ in runner.prompts if "independent reviewer" in p]
    assert len(judge_calls) == 3 and rep.votes == ["pass", "pass", "pass"]
    assert item.judgements["spec"]["votes"] == ["pass", "pass", "pass"]


def test_judge_votes_is_read_from_factory_toml_and_defaults_to_one(factory_root):
    assert load_config(factory_root).judge_votes == 3  # the factory's own setting
    toml = factory_root / "factory.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace("judge_votes = 3\n", ""), encoding="utf-8")
    assert load_config(factory_root).judge_votes == 1
