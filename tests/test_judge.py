import json
from pathlib import Path

import pytest

from factory.agents import AgentResult
from factory.judge import RUBRICS, build_prompt, compute_verdict, evaluate, extract_json, judge

ARTIFACT = """# Spec
- **INV-1**: amounts above 500 EUR MUST carry a receipt URL.
- **BHV-1**: Given an expense of 600 EUR without receipt, When submitted, Then HTTP 422.
"""
IDEA = "Employees submit expenses; above 500 EUR a receipt URL is required."


def answer(scores: dict[str, int], quote: str = "amounts above 500 EUR MUST carry a receipt URL") -> str:
    return json.dumps(
        {
            "criteria": [
                {"id": k, "score": v, "evidence": f"because {k}", "quote": quote} for k, v in scores.items()
            ],
            "summary": "ok",
        }
    )


ALL_FIVE = {cid: 5 for cid, _ in RUBRICS["spec"]}


def test_extract_json_tolerates_prose_and_fences():
    assert extract_json('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json("no json here") is None
    assert extract_json("{broken") is None


@pytest.mark.parametrize(
    ("scores", "verdict"),
    [
        ([5, 5, 4], "pass"),
        ([5, 4, 3], "revise"),
        ([4, 4, 4, 3], "revise"),
        ([5, 5, 2], "fail"),
        ([], "unreliable"),
    ],
)
def test_compute_verdict(scores, verdict):
    assert compute_verdict(scores)[0] == verdict


def test_evaluate_pass_when_grounded():
    rep = evaluate("spec", answer(ALL_FIVE), ARTIFACT)
    assert rep.verdict == "pass" and rep.average == 5 and rep.problems == []
    assert all(c.grounded for c in rep.criteria)


def test_model_cannot_overrule_the_computed_verdict():
    raw = json.loads(answer({**ALL_FIVE, "testability": 1}))
    raw["verdict"] = "pass"  # a flattering verdict from the model is ignored
    assert evaluate("spec", json.dumps(raw), ARTIFACT).verdict == "fail"


def test_ungrounded_quotes_are_discarded_and_make_judge_unreliable():
    rep = evaluate("spec", answer(ALL_FIVE, quote="this sentence is not in the artifact"), ARTIFACT)
    assert rep.verdict == "unreliable"
    assert not any(c.grounded for c in rep.criteria)
    assert any("not found verbatim" in p for p in rep.problems)


def test_quote_matching_ignores_markdown_and_whitespace():
    quote = "amounts above 500 EUR MUST carry a receipt URL"
    assert evaluate("spec", answer(ALL_FIVE, quote=f"**{quote}**"), ARTIFACT).verdict == "pass"
    assert evaluate("spec", answer(ALL_FIVE, quote=quote.replace(" ", "  ")), ARTIFACT).verdict == "pass"


SPEC_LIKE = """
- BHV-1: `GET /health` returns 200 {"status": "ok"}.
  - Given the service is running, When a client calls `GET /health`, Then the response is 200.
- BHV-2: Submit a valid expense.
## 6. Non-goals
- Authentication, authorization, and role checks.
- Editing or deleting expenses.
"""


@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        # real text, exact
        ("BHV-2: Submit a valid expense.", True),
        # a bullet joined to its sub-bullet (the bullet marker is not content)
        ('BHV-1: `GET /health` returns 200 {"status": "ok"}. Given the service is running', True),
        # two real passages stitched with an ellipsis, in order
        ("Authentication, authorization, and role checks ... Editing or deleting expenses.", True),
        ("Authentication, authorization, and role checks … Editing or deleting expenses", True),
        # real fragments but in the wrong order: not an excerpt of the document
        ("Editing or deleting expenses ... Authentication, authorization, and role checks", False),
        # one real fragment plus one fabricated fragment
        ("Authentication, authorization, and role checks ... Billing is handled by the ERP system", False),
        # entirely fabricated, empty, or too short to be evidence
        ("The system supports multi-currency conversion at ingest", False),
        ("", False),
        ("200", False),
        ("... ...", False),
    ],
)
def test_grounding_accepts_real_excerpts_and_rejects_fabrication(quote, expected):
    from factory.judge import _norm, is_grounded

    assert is_grounded(quote, _norm(SPEC_LIKE)) is expected


def test_missing_and_invalid_criteria_are_reported():
    partial = {"fidelity": 5, "testability": 5}
    rep = evaluate("spec", answer(partial), ARTIFACT)
    assert rep.verdict == "unreliable"  # 2 of 6 criteria scored: not enough to trust
    assert any("eval_coverage: not scored" in p for p in rep.problems)
    bad = json.dumps({"criteria": [{"id": "fidelity", "score": 9, "evidence": "x", "quote": "y"}]})
    assert any("not an integer 1-5" in p for p in evaluate("spec", bad, ARTIFACT).problems)
    dup = json.dumps({"criteria": [{"id": "nope", "score": 3, "evidence": "x", "quote": "y"}]})
    assert any("unexpected" in p for p in evaluate("spec", dup, ARTIFACT).problems)


def test_garbage_answer_is_unreliable_not_a_crash():
    rep = evaluate("spec", "I think it is great!", ARTIFACT)
    assert rep.verdict == "unreliable" and "not valid JSON" in rep.problems[0]


class Runner:
    def __init__(self, result: AgentResult) -> None:
        self.result, self.calls = result, []

    def run(self, prompt, **kw):
        self.calls.append((prompt, kw))
        return self.result


def test_judge_end_to_end_with_fake_runner(tmp_path: Path):
    runner = Runner(AgentResult(True, answer(ALL_FIVE), 0.004))
    rep = judge(runner, "spec", ARTIFACT, IDEA, model="haiku", cwd=tmp_path)
    assert rep.verdict == "pass" and rep.cost_usd == 0.004 and rep.model == "haiku"
    prompt, kw = runner.calls[0]
    assert IDEA in prompt and "INV-1" in prompt and kw["model"] == "haiku"
    assert "Short:" not in rep.markdown() and "# Judge report: spec (pass, 5.0/5)" in rep.markdown()
    assert rep.short() == "judge spec: pass (5.0/5)"


def test_judge_runner_failure_is_unreliable(tmp_path: Path):
    rep = judge(Runner(AgentResult(False, "", 0.0, "boom")), "plan", "x", IDEA, model=None, cwd=tmp_path)
    assert rep.verdict == "unreliable" and "boom" in rep.problems[0]


def test_unknown_kind_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown artifact kind"):
        judge(Runner(AgentResult(True, "{}")), "poem", "x", IDEA, model=None, cwd=tmp_path)


@pytest.mark.parametrize("kind", sorted(RUBRICS))
def test_prompt_lists_every_criterion_and_demands_verbatim_quotes(kind):
    prompt = build_prompt(kind, "ARTIFACT-BODY", "IDEA-BODY")
    assert all(cid in prompt for cid, _ in RUBRICS[kind])
    assert "ARTIFACT-BODY" in prompt and "IDEA-BODY" in prompt
    assert "verbatim" in prompt and "ONLY this JSON" in prompt


def test_the_fidelity_rubric_does_not_penalize_what_the_factory_itself_mandates():
    """Regression (calibration): the judge failed a known-good spec for 'inventing /health', which the
    factory's own spec prompt requires. The rubric and the spec prompt must agree."""
    from factory.templates import spec_prompt
    from factory.workitem import WorkItem

    fidelity = dict(RUBRICS["spec"])["fidelity"]
    prompt = spec_prompt(WorkItem(slug="x", title="X", idea="i", maturity="poc"), "")
    assert "GET /health" in prompt and "tech radar" in prompt  # what the factory mandates...
    assert (
        "GET /health" in fidelity and "tech radar" in fidelity and "NOT inventions" in fidelity
    )  # ...is exempt
