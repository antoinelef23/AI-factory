import pytest

from factory.guard import plan_radar_errors
from factory.templates import with_run_log
from tests.test_plan_lint_loop import FakeEngine, Scripted, to_plan

PLAN = """---
type: tasks
---
### T1 — Build the API
- **prompt :**
  > {t1}
### T2 — Storage
- **prompt :**
  > {t2}
### CP-1 — Ship review (the merge is human)
- **trigger :** auto when [T2] done
"""


def plan(t1="Implement the endpoints with FastAPI.", t2="Persist data in PostgreSQL."):
    return PLAN.format(t1=t1, t2=t2)


def test_a_plan_using_only_allowed_technologies_is_clean(radar):
    assert plan_radar_errors(plan(), radar, "poc") == []


def test_a_forbidden_technology_to_use_is_reported_with_its_task_and_the_alternative(radar):
    errors = plan_radar_errors(plan(t2="Store the expenses in MongoDB."), radar, "poc")
    assert errors == ["T2: asks to use MongoDB (hold), not allowed at poc; use PostgreSQL instead"]


@pytest.mark.parametrize(
    "line",
    [
        "Do not add Flask, MongoDB or Requests.",
        "Never use MongoDB: PostgreSQL only.",
        "Avoid Flask; FastAPI is the standard.",
        "Use FastAPI instead of Flask.",
        "Implement it without Requests (use HTTPX).",
        "Flask is forbidden here.",
        "Use FastAPI rather than Flask for routing.",
    ],
)
def test_guardrail_lines_that_name_a_forbidden_technology_to_forbid_it_are_not_usage(radar, line):
    assert plan_radar_errors(plan(t1=line), radar, "prod") == []


def test_each_task_and_technology_is_reported_once(radar):
    text = plan(t1="Use Flask for routing.\nAdd a Flask blueprint.", t2="Use Flask and MongoDB.")
    errors = plan_radar_errors(text, radar, "poc")
    assert [e.split(":")[0] for e in errors] == ["T1", "T2", "T2"]
    assert sum("Flask" in e for e in errors) == 2 and sum("MongoDB" in e for e in errors) == 1


def test_word_boundaries_prevent_false_positives(radar):
    assert (
        plan_radar_errors(
            plan(t1="Write a flasky, mongoose-friendly, requests-per-second test."), radar, "poc"
        )
        == []
    )


def test_only_blocked_technologies_count_not_trial_or_assess(radar):
    # Django is 'trial': allowed at poc, needs IT approval at mvp: a design matter, not a plan error
    assert plan_radar_errors(plan(t1="Use Django for the admin."), radar, "mvp") == []


def test_blocking_depends_on_maturity(radar):
    text = plan(t1="Use SQLite for the prototype.")  # assess: allowed at pov, approval at poc, blocked at mvp
    assert plan_radar_errors(text, radar, "pov") == [] and plan_radar_errors(text, radar, "poc") == []
    assert len(plan_radar_errors(text, radar, "mvp")) == 1


# ------------------------------------------------------------------ in the foreman's lint loop


FORBIDDEN_PLAN = (
    "---\ntype: tasks\nstatus: proposed\n---\n### T1 — Build\n"
    "- **depends_on :** []\n- **prompt :**\n  > Use Flask for routing.\n"
)
GOOD_PLAN = FORBIDDEN_PLAN.replace("Use Flask for routing.", "Use FastAPI for routing.")


def test_a_plan_aiming_at_a_forbidden_stack_is_reprompted_before_any_build(foreman):
    runner = Scripted([FORBIDDEN_PLAN, GOOD_PLAN])
    item = to_plan(foreman, runner, FakeEngine())
    assert foreman.store.read(item, "tasks.md") == with_run_log(GOOD_PLAN) and item.status == "waiting"
    second = [p for p in runner.prompts if "planner of" in p][1]
    assert "T1: asks to use Flask (hold), not allowed at poc; use FastAPI instead" in second


def test_the_radar_check_works_without_claudo_too(foreman):
    runner = Scripted([FORBIDDEN_PLAN, GOOD_PLAN])
    item = to_plan(foreman, runner, None)  # no engine: the radar errors alone drive the retry
    assert foreman.store.read(item, "tasks.md") == with_run_log(GOOD_PLAN)
    assert not (
        foreman.store.dir(item.slug) / "plan-lint.md"
    ).exists()  # final plan clean, no engine: no report


def test_a_persistently_forbidden_plan_blocks_with_the_radar_reason(foreman):
    foreman.cfg.plan_lint_retries = 1
    item = to_plan(foreman, Scripted([FORBIDDEN_PLAN, FORBIDDEN_PLAN]), None)
    assert (item.stage, item.status) == ("plan", "blocked") and "asks to use Flask" in item.feedback


def test_a_clean_plan_without_an_engine_still_writes_no_lint_report(foreman):
    item = to_plan(foreman, Scripted([GOOD_PLAN]), None)
    assert item.status == "waiting" and not (foreman.store.dir(item.slug) / "plan-lint.md").exists()


# ------------------------------------------------------------------ ambiguous names (real false positives)

# Lines copied from agent-written plans in the live runs: "requests" is the English word, not the library.
REAL_PLAN_LINES = [
    "loop 20 requests per endpoint (EVAL-2)",
    "12 non-GET requests returning 405 (INV-4), 404 for /unknown",
    "p95 latency over 100 sequential requests is at most 200 ms for each of the 3 endpoints",
    "send 100 sequential requests with the TestClient, compute p95 latency",
    "100 sequential requests split across the three endpoints, assert 0 non-200 responses",
]


@pytest.mark.parametrize("line", REAL_PLAN_LINES)
def test_the_english_word_requests_is_not_the_requests_library(radar, line):
    assert plan_radar_errors(plan(t1=line), radar, "prod") == []
    assert radar.scan_text(line) == [] or all(t.id != "requests" for t in radar.scan_text(line))


@pytest.mark.parametrize(
    "line",
    [
        "Fetch the page with `requests`.",
        "import requests",
        "from requests import Session",
        "Call requests.get(url) for each item.",
        "Run pip install requests first.",
        "uv add httpx requests",
        "Pin requests>=2.31 in pyproject.",
        "Use the Requests library for HTTP.",
    ],
)
def test_code_like_mentions_of_the_requests_library_are_still_caught(radar, line):
    assert "requests" in [t.id for t in radar.scan_text(line)]  # (a line may name other libraries too)
    assert len(plan_radar_errors(plan(t1=line), radar, "poc")) == 1


def test_a_business_idea_about_customer_requests_does_not_claim_the_library(foreman):
    item = foreman.run(
        foreman.intake("Support", "Agents log and track customer requests and callbacks", "poc")
    )
    assert not any("Requests" in n for n in item.notes)


def test_non_ambiguous_hold_technologies_still_match_in_prose(radar):
    assert [t.id for t in radar.scan_text("Store the data in MongoDB and serve it with Flask")] == [
        "flask",
        "mongodb",
    ]


def test_the_radar_file_marks_only_everyday_words_as_strict(radar):
    assert [t.id for t in radar.techs if t.text_strict] == ["uv", "requests"]


@pytest.mark.parametrize(
    "line",
    [
        "Remove pydantic from pyproject and the code.",
        "Drop Flask, then verify the suite is green.",
        "Delete the MongoDB client module.",
        "Migrate away from Requests: use HTTPX.",
        "Migrate off Flask.",
        "Phase out the legacy Flask blueprints.",
        "Uninstall Requests from the environment.",
        "Eliminate every MongoDB call.",
        "Get rid of Flask.",
    ],
)
def test_a_migration_plan_that_removes_a_forbidden_technology_is_not_using_it(radar, line):
    """Regression: a migration's whole job is to name the technology it removes."""
    assert plan_radar_errors(plan(t1=line), radar, "prod") == []


def test_using_a_forbidden_technology_is_still_caught_next_to_removal_words_in_other_lines(radar):
    text = plan(t1="Remove the old driver.\nStore the data in MongoDB.")
    assert [e.split(":")[0] for e in plan_radar_errors(text, radar, "poc")] == ["T1"]
