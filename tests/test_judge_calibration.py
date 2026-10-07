"""Calibrate the LLM judge against degraded copies of a known-good spec.

Free part: the degradations do what they claim. Billed part (opt-in, `just calibrate`): the judge must
score each degraded spec clearly below the good one on the criteria that degradation targets.
"""

import os
from pathlib import Path

import pytest

from factory.agents import ClaudeRunner
from factory.judge import RUBRICS, CriterionScore, JudgeReport, judge
from tests.calibration import DEGRADATIONS, compare, grounded_scores

FIXTURE = Path(__file__).resolve().parents[1] / "evals" / "judge_calibration" / "ping-service"
SPEC = (FIXTURE / "spec.md").read_text(encoding="utf-8")
IDEA = (FIXTURE / "idea.md").read_text(encoding="utf-8").split("## What the business wants")[1].strip()


# ------------------------------------------------------------ free: the degradations are what they claim


@pytest.mark.parametrize("name", sorted(DEGRADATIONS))
def test_each_degradation_changes_the_spec_and_names_real_criteria(name):
    degrade, targets = DEGRADATIONS[name]
    assert degrade(SPEC) != SPEC
    assert targets and all(t in dict(RUBRICS["spec"]) for t in targets)


def test_no_evals_removes_every_eval_row_and_nothing_else_structural():
    out = DEGRADATIONS["no_evals"][0](SPEC)
    assert "EVAL-1 |" not in out and "## 7. Evals" not in out
    assert "## 6. Non-goals" in out and "BHV-1" in out


def test_no_examples_keeps_the_neighbouring_sections():
    out = DEGRADATIONS["no_examples"][0](SPEC)
    assert "## 5. Examples" not in out and "## 4. Behaviors" in out and "## 6. Non-goals" in out


def test_invented_requirement_adds_exactly_one_rule_the_idea_never_stated():
    out = DEGRADATIONS["invented_requirement"][0](SPEC)
    assert out.count("INV-6") == 1 and "OAuth2" in out and "OAuth2" not in IDEA and "OAuth2" not in SPEC


def test_vague_removes_the_concrete_outcomes():
    out = DEGRADATIONS["vague"][0](SPEC)
    assert "responds appropriately and quickly" in out and '{"pong": true}' in SPEC
    assert out.count("  - And the JSON body equals") == 0


def test_the_baseline_is_a_real_agent_written_spec():
    assert SPEC.count("**INV-") >= 5 and SPEC.count("**BHV-") >= 5 and "## 7. Evals" in SPEC


def test_vague_also_blurs_the_eval_thresholds():
    out = DEGRADATIONS["vague"][0](SPEC)
    rows = [ln for ln in SPEC.splitlines() if ln.startswith("| EVAL-")]
    assert rows and out.count("works well |") == len(rows)  # every eval row lost its threshold
    assert "300 of 300 responses identical" in SPEC and "300 of 300" not in out


# ------------------------------------------------------------ free: judging the judge's comparison logic


def report(scores: dict[str, int], verdict: str = "revise", grounded: bool = True) -> JudgeReport:
    crit = [CriterionScore(k, v, "e", "q", grounded) for k, v in scores.items()]
    avg = sum(scores.values()) / len(scores) if scores else 0.0
    return JudgeReport(kind="spec", criteria=crit, verdict=verdict, average=avg)


def test_compare_caught_when_a_target_score_drops_and_the_average_falls():
    base, bad = report({"eval_coverage": 5, "fidelity": 4}), report({"eval_coverage": 3, "fidelity": 4})
    assert compare(base, bad, ("eval_coverage",))[0] == "caught"


def test_compare_missed_when_the_judge_scores_the_degraded_spec_the_same_or_higher():
    base = report({"unambiguity": 5, "fidelity": 4})
    assert compare(base, report({"unambiguity": 5, "fidelity": 4}), ("unambiguity",))[0] == "missed"
    assert compare(base, report({"unambiguity": 5, "fidelity": 5}), ("unambiguity",))[0] == "missed"


def test_compare_missed_when_the_target_drops_but_the_overall_average_does_not_fall():
    base, bad = report({"testability": 5, "scope": 3}), report({"testability": 4, "scope": 5})
    assert compare(base, bad, ("testability",))[0] == "missed"


def test_compare_is_inconclusive_when_the_baseline_lacks_the_criterion():
    """Regression (calibration run 3): 'examples' was absent from the baseline, giving a bogus drop of -4."""
    base, bad = report({"fidelity": 3}), report({"examples": 4, "fidelity": 3})
    verdict, why = compare(base, bad, ("examples",))
    assert verdict == "inconclusive" and "examples" in why


def test_compare_ignores_ungrounded_scores_and_unreliable_answers():
    base = report({"eval_coverage": 5})
    ungrounded = report({"eval_coverage": 1}, grounded=False)
    assert compare(base, ungrounded, ("eval_coverage",))[0] == "inconclusive"
    assert (
        compare(base, report({"eval_coverage": 1}, verdict="unreliable"), ("eval_coverage",))[0]
        == "inconclusive"
    )
    assert grounded_scores(ungrounded) == {}


def test_compare_uses_the_best_comparable_target_criterion():
    base, bad = report({"testability": 5, "unambiguity": 5}), report({"testability": 5, "unambiguity": 3})
    assert compare(base, bad, ("unambiguity", "testability"))[0] == "caught"


# ------------------------------------------------------------ billed: can the judge tell them apart?


@pytest.mark.live
def test_the_judge_notices_degraded_specs(capsys):
    """Opt-in (`just calibrate`, billed). JUDGE_MODEL picks the model (default: haiku, the factory's default).

    Passing means: the good spec is not failed, and every degradation that can be evaluated was caught."""
    model = os.environ.get("JUDGE_MODEL", "haiku")
    runner = ClaudeRunner()

    def run(text):
        return judge(runner, "spec", text, IDEA, model=model, cwd=FIXTURE)

    base = run(SPEC)
    lines = [f"JUDGE MODEL {model}", f"baseline {base.verdict} {base.average:.2f} {grounded_scores(base)}"]
    cost, outcome = base.cost_usd, {}
    for name, (degrade, targets) in DEGRADATIONS.items():
        rep = run(degrade(SPEC))
        outcome[name] = compare(base, rep, targets)
        cost += rep.cost_usd
        lines.append(f"{name:<22} {outcome[name][0]:<12} {outcome[name][1]}")
        lines.append(f"{'':<22} scores {grounded_scores(rep)} problems {len(rep.problems)}")
    missed = [n for n, (v, _) in outcome.items() if v == "missed"]
    inconclusive = [n for n, (v, _) in outcome.items() if v == "inconclusive"]
    lines.append(
        f"caught {sum(v == 'caught' for v, _ in outcome.values())}/4, missed {missed}, "
        f"inconclusive {inconclusive}, total cost ${cost:.2f}"
    )
    with capsys.disabled():
        print("\n" + "\n".join(lines))
    assert base.verdict in ("pass", "revise"), (
        f"the good spec must not be failed or unreliable: {base.verdict}"
    )
    assert not missed, f"the {model} judge did not notice: {missed}"
