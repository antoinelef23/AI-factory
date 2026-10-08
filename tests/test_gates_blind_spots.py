"""Gates that cannot be blinded (review findings G-1 .. G-6)."""

import subprocess
from pathlib import Path

import pytest

from factory.detect import scan_project
from factory.gates import secrets_gate
from factory.guard import check_project, plan_scope_errors
from factory.project import RUN_LOG_ROW, modified_tests, prepare_project
from tests.test_change_flow import ChangeAgent, shipped_app, to_plan_review
from tests.test_gates_immutable_trajectory import run_to_build


def write(root: Path, rel: str, text: str | bytes) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def violation(report, key):
    return next((v for v in report.violations if v.key == key), None)


# ------------------------------------------------------------------ G-1: manifests that cannot be read


def test_a_broken_pyproject_blocks_instead_of_reading_as_no_dependencies(tmp_path, radar):
    write(tmp_path, "pyproject.toml", "[project\ndependencies = [flask")
    report = check_project(tmp_path, radar, "poc")
    v = violation(report, "unparseable:pyproject.toml")
    assert v is not None and v.verdict == "block" and "cannot be parsed" in v.hint
    assert not report.ok()


def test_an_undecodable_requirements_file_blocks_too(tmp_path, radar):
    write(tmp_path, "requirements.txt", b"\xff\xfe\x00flask\n")
    assert violation(check_project(tmp_path, radar, "pov"), "unparseable:requirements.txt")


def test_a_valid_manifest_with_the_wrong_types_is_reported_not_crashed(tmp_path, radar):
    write(tmp_path, "pyproject.toml", '[project]\ndependencies = 3\n[tool.poetry]\ndependencies = "x"\n')
    assert violation(check_project(tmp_path, radar, "poc"), "unparseable:pyproject.toml")


def test_poetry_dependencies_are_seen(tmp_path, radar):
    write(
        tmp_path,
        "pyproject.toml",
        '[tool.poetry.dependencies]\npython = "^3.12"\nflask = "*"\n'
        '[tool.poetry.group.dev.dependencies]\npytest = "*"\n',
    )
    names = {f.name for f in scan_project(tmp_path)}
    assert {"flask", "pytest"} <= names and "python" not in names
    assert violation(check_project(tmp_path, radar, "poc"), "flask").verdict == "block"


@pytest.mark.parametrize(
    "name",
    ["go.mod", "pom.xml", "build.gradle", "build.gradle.kts", "Cargo.toml", "Gemfile", "composer.json"],
)
def test_unanalysed_ecosystems_need_it_review_and_an_exception_lifts_it(tmp_path, radar, name):
    write(tmp_path, name, "anything\n")
    report = check_project(tmp_path, radar, "mvp")
    v = violation(report, f"unanalysed:{name}")
    assert v is not None and v.verdict == "needs_it_approval" and not report.ok()
    assert report.ok({f"unanalysed:{name}"})


# ------------------------------------------------------------------ G-2: secrets the gate used to miss

AWS = "AKIA" + "ABCDEFGHIJKLMNOP"  # every token is assembled at runtime: no token-shaped literal in the repo


@pytest.mark.parametrize(
    ("filename", "content", "label"),
    [
        ("deploy.sh", f"export AWS_KEY={AWS}\n", "AWS"),
        ("notes.txt", f"key {AWS}\n", "AWS"),
        ("Dockerfile", f"ENV KEY={AWS}\n", "AWS"),
        ("server.pem", "-----BEGIN " + "PRIVATE KEY-----\n", "private key"),
        ("id_rsa", "-----BEGIN " + "RSA PRIVATE KEY-----\n", "private key"),
        (".npmrc", "//registry.example/:_authToken=" + "ghp_" + "A" * 36 + "\n", "GitHub token"),
        ("main.tf", 'token = "' + "github_pat_" + "A" * 70 + '"\n', "GitHub fine-grained"),
        ("run.ps1", "$t = '" + "xoxb-" + "1" * 12 + "'\n", "Slack"),
        ("cfg.xml", "<key>" + "AIza" + "A" * 35 + "</key>\n", "Google"),
        ("settings.conf", "stripe=" + "sk_live_" + "a" * 24 + "\n", "Stripe"),
        ("nb.ipynb", f'{{"cells": ["{AWS}"]}}\n', "AWS"),
    ],
)
def test_secrets_gate_catches_these_files_and_token_types(tmp_path, filename, content, label):
    write(tmp_path, filename, content)
    result = secrets_gate(tmp_path)
    assert not result.ok and label in result.detail and filename in result.detail


def test_ordinary_files_stay_clean(tmp_path):
    write(tmp_path, "notes.txt", "nothing to see\nthe key is in the vault\n")
    write(tmp_path, "run.sh", "echo hello\n")
    assert secrets_gate(tmp_path).ok


# ------------------------------------------------------------------ G-3: git-tracked folders are scanned


@pytest.fixture
def repo(tmp_path):
    dest = tmp_path / "repo"
    dest.mkdir()
    write(dest, "pyproject.toml", '[project]\ndependencies = ["fastapi"]\n')
    prepare_project(dest)
    return dest


def test_a_tracked_build_folder_is_scanned(repo, radar):
    write(repo, "build/evil.py", "import flask\n")
    git(repo, "add", "-f", "-A")
    git(repo, "commit", "-q", "-m", "vendored code")
    assert violation(check_project(repo, radar, "poc"), "flask").verdict == "block"


def test_a_tracked_dist_folder_is_secret_scanned(repo):
    write(repo, "dist/app.py", f'KEY = "{AWS}"\n')
    git(repo, "add", "-f", "-A")
    git(repo, "commit", "-q", "-m", "built output")
    assert not secrets_gate(repo).ok


def test_untracked_junk_folders_are_still_skipped(repo, radar):
    write(repo, ".venv/lib/x.py", "import flask\n")  # ignored by the golden path .gitignore
    write(repo, "dist/untracked.py", "import flask\n")  # untracked and in a junk folder
    assert violation(check_project(repo, radar, "poc"), "flask") is None


def test_a_folder_that_is_not_a_git_project_is_walked_as_before(tmp_path, radar):
    write(tmp_path, "build/evil.py", "import flask\n")  # skipped by the walk, as it always was
    write(tmp_path, "app/ok.py", "import fastapi\n")
    assert violation(check_project(tmp_path, radar, "poc"), "flask") is None


# ------------------------------------------------------------------ G-4: tests rewritten by a change


def test_modified_tests_lists_rewrites_and_deletions_but_not_additions(repo):
    write(repo, "tests/test_a.py", "def test_a(): pass\n")
    write(repo, "tests/test_b.py", "def test_b(): pass\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "tests")
    base = git(repo, "rev-parse", "HEAD")
    write(repo, "tests/test_a.py", "def test_a(): assert True\n")
    (repo / "tests" / "test_b.py").unlink()
    write(repo, "tests/test_new.py", "def test_new(): pass\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "change")
    assert modified_tests(repo, base) == ["tests/test_a.py (modified)", "tests/test_b.py (deleted)"]


def test_an_unknown_base_is_not_a_crash(repo):
    assert modified_tests(repo, "0" * 40) == []


class RewritesAnExistingTest(ChangeAgent):
    def run(self, prompt, **kw):
        if "implementer" in prompt:
            test = kw["cwd"] / "tests" / "test_health.py"
            test.write_text(test.read_text(encoding="utf-8") + "\n# weakened\n", encoding="utf-8")
        return super().run(prompt, **kw)


def test_a_change_that_rewrites_an_existing_test_needs_it_to_acknowledge(foreman):
    from factory.foreman import FactoryError

    app = shipped_app(foreman)
    foreman.runner = RewritesAnExistingTest(edit=False)
    ch = to_plan_review(foreman, foreman.intake_change(app.slug, "Add a thing", "add a thing", "feature"))
    ch = foreman.approve(ch, "owner")
    assert ch.stage == "ship_review", ch.feedback
    assert [a["kind"] for a in ch.ship_acks] == ["tests_modified"]
    assert "tests/test_health.py (modified)" in ch.ship_acks[0]["detail"]
    with pytest.raises(FactoryError, match="tests_modified.*--note"):
        foreman.approve(ch, "it", by="bob")


def test_a_change_that_only_adds_tests_raises_nothing(foreman):
    app = shipped_app(foreman)
    foreman.runner = ChangeAgent(edit=False)
    ch = to_plan_review(foreman, foreman.intake_change(app.slug, "Add a thing", "add a thing", "feature"))
    ch = foreman.approve(ch, "owner")
    assert ch.stage == "ship_review" and ch.ship_acks == []


# ------------------------------------------------------------------ G-5: the approved plan is protected


def append_row(app_triplet, _store):
    with (app_triplet / "tasks.md").open("a", encoding="utf-8") as f:
        f.write("| 2026-10-08 07:09 | T1 | implementer | done, evals green (t1) | |\n")


def test_run_log_rows_are_not_a_change_of_the_plan(foreman):
    item = run_to_build(foreman, tamper=append_row)
    assert (item.stage, item.status) == ("ship_review", "waiting"), item.feedback


def test_editing_a_task_after_the_owner_approved_is_caught(foreman):
    def rewrite(app_triplet, _store):
        plan = app_triplet / "tasks.md"
        plan.write_text(plan.read_text(encoding="utf-8").replace("###", "### (edited)", 1), encoding="utf-8")

    item = run_to_build(foreman, tamper=rewrite)
    assert item.status == "blocked" and "tasks.md: modified inside the app" in item.feedback


def test_a_plan_edited_in_the_store_after_approval_is_caught(foreman):
    def amend(_app, store_dir):
        plan = store_dir / "tasks.md"
        plan.write_text(plan.read_text(encoding="utf-8") + "\n### T9 - sneaky\n", encoding="utf-8")

    item = run_to_build(foreman, tamper=amend)
    assert item.status == "blocked" and "tasks.md: changed in work/x/ after its approval" in item.feedback


def test_the_run_log_regex_matches_what_claudo_writes():
    assert RUN_LOG_ROW.match("| 2026-10-08 07:09 | CP-1 | owner | checkpoint validated | |")
    assert not RUN_LOG_ROW.match("| T1 | a task |")


# ------------------------------------------------------------------ G-6: a plan against its own scope

REAL_T2 = """### T2 - Method and near-miss path tests for /about
- **depends_on :** [T1]
- **implements :** [BHV-3, BHV-4, EVAL-7, EVAL-8, EX-2, EX-3]
- **files_touched :** `tests/test_about_routes.py`
- **verify :** `uv run pytest -q`
- **done_when :** `POST`, `PUT` and `DELETE` on `/about` return 405, and the full suite is green.
- **prompt :**
  > Create the new file `tests/test_about_routes.py`. Do not modify any existing test file.
  > Write these tests, each marked `@pytest.mark.eval`:
  > - `test_eval_7_about_unsupported_methods_405`: `POST`, `PUT` and `DELETE` on `/about` each return 405.
  > Make sure `/about/` does not redirect: the test client must not follow redirects.
  > If `GET /about/` returns a 307 redirect, set `redirect_slashes=False` on the FastAPI app in `app/main.py`. Check first that this does not change the existing endpoints.
  > If the tests pass without any app change, leave `app/main.py` alone. If `app/main.py` must change, keep the diff minimal and mention `app/main.py` in the commit message.
  > Run `uv run pytest -q` and `uv run ruff check .`. Both must pass.
"""  # noqa: E501


def test_the_real_t2_that_caused_the_regression_is_caught_once():
    assert plan_scope_errors(REAL_T2) == [
        "T2: the prompt changes app/main.py but files_touched does not list it"
    ]


@pytest.mark.parametrize(
    "prompt",
    [
        "Read `app/main.py` first, then create `tests/test_x.py`.",
        "Do not modify `app/main.py`.",
        "Never edit `pyproject.toml`; add no dependency.",
        "Run `uv run pytest -q`.",
    ],
)
def test_reading_forbidding_and_running_are_not_changes(prompt):
    plan = f"### T1 - X\n- **files_touched :** `tests/test_x.py`\n- **prompt :**\n  > {prompt}\n"
    assert plan_scope_errors(plan) == []


def test_directories_and_globs_in_files_touched_cover_their_files():
    plan = (
        "### T1 - X\n- **files_touched :** `app/`, `tests/test_*.py`\n- **prompt :**\n"
        "  > Edit `app/main.py` and create `tests/test_x.py`; update `README.md`.\n"
    )
    assert plan_scope_errors(plan) == ["T1: the prompt changes README.md but files_touched does not list it"]


def test_checkpoint_blocks_are_not_tasks():
    plan = "### CP-1 - Review\n- **prompt :**\n  > Edit `app/main.py`.\n"
    assert plan_scope_errors(plan) == []


def test_the_scope_check_feeds_the_plan_relint_loop(foreman):
    item = foreman.intake("X", "an api", "poc")
    result = foreman._lint(item, REAL_T2)
    assert result is not None and any("app/main.py" in e for e in result.errors)
    assert (
        foreman._lint(item, "### T1 - ok\n- **files_touched :** `a.py`\n- **prompt :**\n  > Edit `a.py`.\n")
        is None
    )
