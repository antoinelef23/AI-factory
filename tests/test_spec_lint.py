from pathlib import Path

import pytest

from factory.speclint import lint_spec
from factory.templates import offline_change_spec, offline_spec
from factory.workitem import WorkItem
from tests.calibration import DEGRADATIONS
from tests.conftest import VALID_CHANGE_SPEC, VALID_SPEC, FakeAgentRunner

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REAL = {
    n: (FIXTURES / n).read_text(encoding="utf-8") for n in ("spec-ping-service.md", "spec-expense-tracker.md")
}


def errors(text, kind="app"):
    return lint_spec(text, kind=kind).errors


# ------------------------------------------------------------------ real and template specs are clean


@pytest.mark.parametrize("name", sorted(REAL))
def test_real_agent_written_specs_lint_clean_whatever_their_style(name):
    """Two real specs from live runs, in different styles (bold IDs / plain IDs): no false positive."""
    assert lint_spec(REAL[name]).errors == []


def test_the_factorys_own_offline_templates_lint_clean():
    """Regression: the linter found that the offline spec never covered BHV-2 with an eval."""
    new = WorkItem(slug="x", title="X", idea="an api", maturity="poc")
    change = WorkItem(slug="y", title="Y", idea="change it", maturity="poc", kind="migration", target="x")
    assert lint_spec(offline_spec(new)).errors == []
    assert lint_spec(offline_change_spec(change), kind="migration").errors == []
    assert lint_spec(VALID_SPEC).ok and lint_spec(VALID_CHANGE_SPEC, kind="feature").ok


# ------------------------------------------------------------------ what it catches (the judge's blind spots)


def test_a_deleted_evals_section_is_caught_deterministically():
    out = errors(DEGRADATIONS["no_evals"][0](REAL["spec-ping-service.md"]))
    assert any("'## 7. Evals' is missing" in e for e in out) and any(
        "section 7 has no eval" in e for e in out
    )


def test_a_deleted_examples_section_is_caught():
    out = errors(DEGRADATIONS["no_examples"][0](REAL["spec-ping-service.md"]))
    assert out == ["section '## 5. Examples' is missing"]


def test_the_lint_does_not_pretend_to_judge_meaning():
    """Vague outcomes and invented requirements are semantic: the LLM judge's job, not a parser's."""
    base = REAL["spec-ping-service.md"]
    assert errors(DEGRADATIONS["vague"][0](base)) == []
    # The calibration's invention ships WITHOUT an eval, so the lint flags it as uncovered...
    invented = DEGRADATIONS["invented_requirement"][0](base)
    assert any("INV-6" in e and "not covered" in e for e in errors(invented))
    # ...but only by that side effect: give the same invention an eval row and it is structurally flawless.
    row = "| EVAL-13 | Automated test | Check OAuth2 login on every endpoint. | INV-6 | 1 of 1 pass |\n"
    assert errors(invented.rstrip("\n") + "\n" + row) == []


def test_an_invariant_or_behavior_no_eval_covers_is_named():
    cleaned = "\n".join(ln for ln in VALID_SPEC.splitlines() if "a test proves the main use case" not in ln)
    out = errors(cleaned)
    assert any("not covered by any eval" in e and "BHV-2" in e for e in out)


def test_a_sub_case_like_bhv_1a_is_covered_through_its_parent():
    spec = VALID_SPEC.replace(
        "**Given** the application is running", "**Given** the application is running (BHV-1a)"
    )
    assert errors(spec) == []


def test_the_same_id_defined_twice_is_an_error():
    spec = VALID_SPEC.replace("### BHV-2: core use case", "### BHV-1: core use case")
    assert any("BHV-1 is defined 2 times" in e for e in errors(spec))


def test_an_eval_citing_an_undefined_id_is_a_warning_not_an_error():
    spec = VALID_SPEC.replace("| BHV-2 | 100% |", "| BHV-2, BHV-77 | 100% |")
    result = lint_spec(spec)
    assert result.ok and any("covers BHV-77, which is not defined" in w for w in result.warnings)


def test_an_eval_with_an_empty_covers_cell_is_a_warning():
    spec = VALID_SPEC.replace("| BHV-2 | 100% |", "|  | 100% |")
    result = lint_spec(spec)
    assert any("EVAL-4 covers nothing" in w for w in result.warnings)
    assert any("BHV-2" in e and "not covered" in e for e in result.errors)  # and BHV-2 is now uncovered


def test_the_covers_column_is_found_by_its_header_not_its_position():
    spec = VALID_SPEC.replace(
        "| ID | Type | Description | Covers | Success threshold |",
        "| ID | Covers | Type | Description | Success threshold |",
    )
    # the data rows keep the old order, so Covers points at the wrong cell: the lint must notice, not crash
    assert any("not covered" in e for e in errors(spec))


# ------------------------------------------------------------------ what the factory mandates


def test_a_new_app_spec_must_have_the_health_behavior():
    spec = VALID_SPEC.replace("/health", "/status")
    assert any("GET /health" in e for e in errors(spec))
    assert not any("GET /health" in e for e in errors(spec, kind="feature"))  # a change does not need one


def test_every_spec_must_tie_the_app_to_the_tech_radar():
    spec = VALID_SPEC.replace("tech radar", "list of approved things")
    assert any("tech radar" in e for e in errors(spec))


def test_a_change_spec_must_state_no_regression():
    spec = VALID_CHANGE_SPEC.replace("no regression", "fine").replace("Existing behavior", "Behavior")
    spec = spec.replace("existing test suite", "suite").replace("regression", "x")
    assert any("no regression" in e for e in errors(spec, kind="feature"))


def test_missing_sections_are_each_named():
    out = errors("# Spec\n\n## 1. Intent\nx\n")
    for name in ("Glossary", "Invariants", "Behaviors", "Examples", "Non-goals", "Evals"):
        assert any("'## " in e and name in e for e in out), name


def test_the_feedback_lists_errors_then_warnings():
    result = lint_spec(VALID_SPEC.replace("| BHV-2 | 100% |", "| BHV-2, BHV-77 | 100% |"))
    assert result.feedback() == "- (warning) EVAL-4 covers BHV-77, which is not defined in this spec"


# ------------------------------------------------------------------ in the foreman


BAD_SPEC = "# Spec\n\n## 1. Intent\n\nSome intent.\n\n## 3. Invariants\n- **INV-1**: radar compliant\n"


class SpecAgent(FakeAgentRunner):
    """Answers spec prompts from a queue (then valid specs), records every prompt."""

    def __init__(self, specs):
        super().__init__()
        self.specs = list(specs)

    def run(self, prompt, **kw):
        from factory.agents import AgentResult

        if "spec writer" in prompt and self.specs:
            self.prompts.append((prompt, kw))
            return AgentResult(True, self.specs.pop(0), 0.01)
        return super().run(prompt, **kw)


def spec_prompts(agent):
    return [p for p, _ in agent.prompts if "spec writer" in p]


def test_a_failing_agent_spec_is_reprompted_with_the_errors_and_its_previous_text(foreman):
    agent = SpecAgent([BAD_SPEC, VALID_SPEC])
    foreman.runner = agent
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    assert (item.stage, item.status) == ("spec_review", "waiting")
    assert foreman.store.read(item, "spec.md").strip() == VALID_SPEC.strip()
    first, second = spec_prompts(agent)
    assert "FAILED the structural lint" not in first
    assert "FAILED the structural lint" in second and "'## 2. Glossary' is missing" in second
    assert "Some intent." in second  # the previous attempt travels with the errors
    assert [h["event"] for h in item.history].count("lint") == 1


def test_a_valid_agent_spec_needs_no_retry(foreman):
    agent = SpecAgent([VALID_SPEC])
    foreman.runner = agent
    foreman.run(foreman.intake("X", "an api", "poc"))
    assert len(spec_prompts(agent)) == 1


def test_spec_retries_are_bounded_then_the_item_blocks_and_keeps_the_spec_for_inspection(foreman):
    foreman.cfg.spec_lint_retries = 2
    agent = SpecAgent([BAD_SPEC, BAD_SPEC, BAD_SPEC, VALID_SPEC])
    foreman.runner = agent
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    assert (item.stage, item.status) == ("spec", "blocked")
    assert len(spec_prompts(agent)) == 3 and agent.specs == [VALID_SPEC]  # first try + 2 retries, no more
    assert "spec failed the structural lint (3 attempt(s))" in item.feedback
    assert foreman.store.read(item, "spec.md") == BAD_SPEC


def test_zero_retries_means_one_attempt(foreman):
    foreman.cfg.spec_lint_retries = 0
    agent = SpecAgent([BAD_SPEC, VALID_SPEC])
    foreman.runner = agent
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    assert item.status == "blocked" and len(spec_prompts(agent)) == 1


def test_business_feedback_survives_the_lint_retries(foreman):
    agent = SpecAgent([VALID_SPEC, BAD_SPEC, VALID_SPEC])
    foreman.runner = agent
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    item = foreman.reject(item, "business", "add a KPI of 3 days")
    foreman.run(item)
    third = spec_prompts(agent)[-1]
    assert "add a KPI of 3 days" in third and "FAILED the structural lint" in third


def test_offline_specs_always_pass_so_offline_never_blocks_on_the_lint(foreman):
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    assert item.status == "waiting" and "lint" not in [h["event"] for h in item.history]


def test_the_spec_prompts_state_the_coverage_rule_up_front(foreman):
    from factory.templates import change_spec_prompt, spec_prompt

    new = WorkItem(slug="x", title="X", idea="i", maturity="poc")
    change = WorkItem(slug="y", title="Y", idea="i", maturity="poc", kind="bug", target="x")
    for prompt in (spec_prompt(new, ""), change_spec_prompt(change, "", "")):
        assert "Covers column of at least one EVAL row" in prompt and "defined exactly once" in prompt


def test_warnings_are_kept_for_the_reviewer(foreman):
    with_warning = VALID_SPEC.replace("| BHV-2 | 100% |", "| BHV-2, BHV-77 | 100% |")
    agent = SpecAgent([with_warning])
    foreman.runner = agent
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    assert item.status == "waiting"
    assert "BHV-77" in foreman.store.read(item, "spec-lint.md")


def test_the_config_default_and_clamping(factory_root):
    from factory.config import load_config

    assert load_config(factory_root).spec_lint_retries == 2
    toml = factory_root / "factory.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace("spec_lint_retries = 2", "spec_lint_retries = -4")
    )
    assert load_config(factory_root).spec_lint_retries == 0
