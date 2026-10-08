"""The factory's artifacts against the REAL Claudo engine.

These tests run Claudo's own plan-lint, so they only run where a Claudo checkout is discoverable
(skipped in CI, enforced locally). They are the proof that "a plan the factory writes is a plan
Claudo executes", not an assumption.
"""

from pathlib import Path

import pytest

from factory.claudo import ClaudoEngine, discover
from factory.templates import offline_tasks

REPO = Path(__file__).resolve().parents[1]
HOME = discover(None, REPO)
pytestmark = pytest.mark.skipif(HOME is None, reason="no Claudo checkout found (set CLAUDO_HOME)")


@pytest.fixture(scope="module")
def engine():
    return ClaudoEngine(HOME)


def offline_triplet(foreman, idea="an api to record expenses", maturity="poc"):
    item = foreman.run(foreman.intake("Expense log", idea, maturity=maturity))
    item = foreman.approve(item, "business")  # design + plan written offline
    s = foreman.store
    return item, s.read(item, "spec.md"), s.read(item, "design.md"), s.read(item, "tasks.md")


def test_offline_plan_lints_clean_with_the_real_engine(foreman, engine):
    item, spec, design, tasks = offline_triplet(foreman)
    result = engine.lint_plan(item.slug, spec=spec, design=design, tasks=tasks)
    assert result.errors == [], result.raw
    assert result.warnings == [], result.raw  # a clean plan: not even advisory noise


@pytest.mark.parametrize("maturity", ["pov", "poc", "mvp", "prod"])
def test_offline_plan_lints_clean_at_every_maturity(foreman, engine, maturity):
    item, spec, design, tasks = offline_triplet(foreman, maturity=maturity)
    # MVP+ waits for IT's design review before the plan exists: write the plan directly.
    tasks = tasks or offline_tasks(item, "python-fastapi")
    result = engine.lint_plan(item.slug, spec=spec, design=design, tasks=tasks)
    assert result.ok, result.raw


def test_the_engine_rejects_what_it_should(foreman, engine):
    """The bridge is not a rubber stamp: a broken plan is reported with Claudo's own messages."""
    item, spec, design, tasks = offline_triplet(foreman)
    # T2 depends on T1 already; making T1 depend on T2 closes a cycle
    cyclic = tasks.replace("- **depends_on :** []", "- **depends_on :** [T2]", 1)
    bad = engine.lint_plan(item.slug, spec=spec, design=design, tasks=cyclic)
    assert not bad.ok and any("cycle" in e for e in bad.errors)
    assert "[BHV-1, BHV-2, INV-1]" in tasks  # guard: the replacement below must actually change the plan
    ghost = tasks.replace("[BHV-1, BHV-2, INV-1]", "[BHV-99]", 1)
    assert any(
        "BHV-99" in e for e in engine.lint_plan(item.slug, spec=spec, design=design, tasks=ghost).errors
    )
    wrong_format = tasks.replace("### T1 — ", "### T1: ").replace("### T2 — ", "### T2: ")
    unreadable = engine.lint_plan(item.slug, spec=spec, design=design, tasks=wrong_format)
    assert any("depends_on" in e or "does not exist" in e or "no task" in e for e in unreadable.errors)


def test_the_agent_written_plan_from_the_live_run_was_unreadable_by_claudo(engine):
    """Regression for the finding that motivated this work: a real agent plan in a different format
    parsed to 0 nodes. Kept as a literal so the prompt change can be judged against it."""
    live_style = "# Tasks: X\n\n### T1: Scaffold\n\n- agent: scaffolder\n- depends_on: []\n"
    result = engine.lint_plan(
        "x", spec="---\nversion: 0.1.0\n---\n- **INV-1** — x\n", design="d", tasks=live_style
    )
    assert not result.ok and "no task recognized" in result.errors[0]


def test_a_plan_ending_in_a_run_log_still_lints_clean_with_the_real_engine(foreman, engine):
    """L-5: the run-log table the factory appends must not trip Claudo's parser, before or after Claudo
    writes its own rows into it."""
    from factory.templates import with_run_log

    item, spec, design, tasks = offline_triplet(foreman)
    assert "## Run log" in tasks  # the factory writes it itself
    row = "| 2026-10-08 07:09 | CP-1 | owner | checkpoint validated | |\n"
    for text in (with_run_log(tasks), with_run_log(tasks) + row):
        result = engine.lint_plan(item.slug, spec=spec, design=design, tasks=text)
        assert result.errors == [] and result.warnings == [], result.raw
