"""Git helpers, plan scope, spec lint and prompts (A139, A140, A153-A155, A162-A166, A169, A171-A173)."""

import subprocess

import pytest

from factory.guard import plan_radar_errors, plan_scope_errors
from factory.project import ProjectError, begin_change, config_get, prepare_project
from factory.speclint import lint_spec
from factory.templates import change_build_prompt, with_run_log
from factory.workitem import WorkItem
from tests.conftest import VALID_SPEC
from tests.test_plan_radar import plan


def git(app, *args):
    return subprocess.run(["git", *args], cwd=app, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def app(tmp_path):
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
    prepare_project(tmp_path)
    return tmp_path


# ------------------------------------------------------------------ git helpers


def test_untracking_runtime_state_leaves_the_operators_staged_work_alone(app):
    state = app / "work" / "x" / ".runs" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text("{}", encoding="utf-8")
    git(app, "add", "-f", "--", str(state.relative_to(app)))
    git(app, "commit", "-qm", "a run committed its state")
    (app / "mine.txt").write_text("staged by the operator\n", encoding="utf-8")
    git(app, "add", "mine.txt")
    prepare_project(app)  # untracks the runtime state
    assert git(app, "show", "--name-only", "--format=", "HEAD") == "work/x/.runs/state.json"
    assert git(app, "diff", "--cached", "--name-only") == "mine.txt"  # still staged, not swept in
    assert state.is_file()


def test_a_missing_config_key_reads_as_empty(app):
    assert config_get(app, "branch.factory/x.factory-base") == ""
    git(app, "config", "branch.factory/x.factory-base", "main")
    assert config_get(app, "branch.factory/x.factory-base") == "main"


def test_a_change_is_never_started_on_a_detached_head(app):
    git(app, "checkout", "-q", "--detach")
    with pytest.raises(ProjectError, match="detached HEAD"):
        begin_change(app, "x")


# ------------------------------------------------------------------ plan scope and radar


def scoped(files, prompt):
    return (
        "### T1 — Do it\n- **depends_on :** []\n"
        f"- **files_touched :** {files}\n- **prompt :**\n  > {prompt}\n"
    )


def test_a_dotfile_in_files_touched_keeps_its_dot():
    assert plan_scope_errors(scoped("`.github/workflows/ci.yml`", "Update `.github/workflows/ci.yml`.")) == []
    assert plan_scope_errors(scoped("`./app/main.py`", "Update `./app/main.py`.")) == []


def test_deleting_a_file_outside_the_scope_is_flagged():
    assert plan_scope_errors(scoped("`app/main.py`", "Delete `app/legacy.py`.")) == [
        "T1: the prompt changes app/legacy.py but files_touched does not list it"
    ]
    assert plan_scope_errors(scoped("`app/main.py`", "Do not modify `app/legacy.py`.")) == []


def test_a_use_next_to_an_unrelated_negation_is_still_a_use(radar):
    assert plan_radar_errors(plan(t1="Use Flask for the API (no auth needed)."), radar, "poc")
    assert plan_radar_errors(plan(t1="Do not add Flask, MongoDB or Requests."), radar, "poc") == []


# ------------------------------------------------------------------ spec lint


def test_an_invariant_defined_in_another_form_still_needs_an_eval():
    spec = VALID_SPEC.replace(
        "## 4. Behaviors", "- **INV-7** (security) tokens expire after 15 min\n\n## 4. Behaviors"
    )
    result = lint_spec(spec)
    assert not result.ok and any("INV-7" in e for e in result.errors)


def test_an_eval_covering_an_undefined_example_is_warned_about():
    spec = VALID_SPEC.replace("| EVAL-1 |", "| EVAL-1 |", 1)
    rows = [ln for ln in spec.splitlines() if ln.startswith("| EVAL-1 |")]
    cells = rows[0].split("|")
    cells[4] = " BHV-1, EX-99 "
    spec = spec.replace(rows[0], "|".join(cells))
    assert any("EX-99, which is not defined" in w for w in lint_spec(spec).warnings)


def test_mandated_content_must_be_in_its_section_not_only_in_non_goals():
    spec = VALID_SPEC.replace("tech radar", "approved list").replace("/health", "/status")
    spec = spec.replace("## 6. Non-goals", "## 6. Non-goals\n\n- No tech radar review, no /health endpoint.")
    errors = lint_spec(spec).errors
    assert any("tech radar" in e for e in errors) and any("/health" in e for e in errors)


# ------------------------------------------------------------------ prompts


def test_the_run_log_heading_is_detected_as_a_heading_not_a_mention():
    plan_text = "### T1 — Show the ## Run log in the admin page\n"
    assert with_run_log(plan_text).count("## Run log") == 2  # the heading was added
    assert with_run_log(with_run_log(plan_text)) == with_run_log(plan_text)


def test_a_migration_may_add_the_replacement_and_nothing_else():
    migration = WorkItem(slug="m", title="M", idea="i", maturity="poc", kind="migration", target="orders")
    feature = WorkItem(slug="f", title="F", idea="i", maturity="poc", kind="feature", target="orders")
    assert "plus the replacement design.md names" in change_build_prompt(migration, [], "")
    assert "Never add a dependency" not in change_build_prompt(migration, [], "")
    assert "Never add a dependency" in change_build_prompt(feature, [], "")


def test_a_long_file_list_says_it_is_truncated(foreman):
    from tests.test_change_flow import shipped_app

    app = shipped_app(foreman)
    for n in range(90):
        (foreman.app_dir(app) / f"extra_{n:02}.txt").write_text("x\n", encoding="utf-8")
    change = foreman.intake_change(app.slug, "Add", "add a thing")
    files = foreman._scaffold_files(change)
    assert len(files) == 81 and files[-1].endswith("more files: list truncated)")


def test_a_change_is_specced_against_the_app_as_it_stands(foreman, tmp_path):
    from tests.test_change_flow import full_migration

    app, merged = full_migration(foreman, tmp_path)
    foreman.merge(merged, "it")
    nxt = foreman.intake_change(app.slug, "Next", "another change")
    current = foreman._current_app_spec(nxt)
    assert f"merged change {merged.slug} (migration)" in current
    assert current.index("Spec") < current.index(f"merged change {merged.slug}")
