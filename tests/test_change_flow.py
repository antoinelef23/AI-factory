import subprocess

import pytest

from factory.agents import AgentResult
from factory.cli import main
from factory.drift import scan_drift
from factory.foreman import FactoryError
from factory.project import current_branch, porcelain
from tests.conftest import VALID_CHANGE_SPEC
from tests.test_claudo_build import ScriptedClaudo
from tests.test_drift import radar_with, ship

PLAN = (
    "---\ntype: tasks\nstatus: proposed\n---\n### T1 - Change\n- **depends_on :** []\n"
    "- **prompt :**\n  > Make the change.\n"
)


def git(app, *args):
    return subprocess.run(
        ["git", *args], cwd=app, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


class ChangeAgent:
    """Answers spec and plan prompts; as build agent it REALLY edits the app (drops the pydantic line)."""

    name = "claude"

    def __init__(self, edit=True):
        self.edit, self.prompts = edit, []

    def run(self, prompt, **kw):
        self.prompts.append(prompt)
        if "spec writer" in prompt:
            return AgentResult(True, VALID_CHANGE_SPEC, 0.01)
        if "planner of" in prompt:
            return AgentResult(True, PLAN, 0.01)
        if self.edit and "implementer" in prompt:
            pyproject = kw["cwd"] / "pyproject.toml"
            text = pyproject.read_text(encoding="utf-8")
            pyproject.write_text(
                "\n".join(ln for ln in text.splitlines() if "pydantic" not in ln) + "\n", encoding="utf-8"
            )
        return AgentResult(True, "changed", 0.02)


def shipped_app(foreman, maturity="poc"):
    return ship(foreman, maturity=maturity)


def to_plan_review(foreman, change):
    change = foreman.run(change)
    change = foreman.approve(change, "business")
    if change.stage == "design_review":
        change = foreman.approve(change, "it")
    return change


# ------------------------------------------------------------------ intake


def test_a_change_inherits_the_apps_maturity_and_targets_its_folder(foreman):
    app = shipped_app(foreman, "poc")
    ch = foreman.intake_change(app.slug, "Add a thing", "add a thing to the app", "feature")
    assert (ch.kind, ch.target, ch.maturity) == ("feature", app.slug, "poc")
    assert foreman.app_dir(ch) == foreman.cfg.apps_dir / app.slug  # NOT apps/<change slug>
    assert (foreman.store.dir(ch.slug) / "idea.md").is_file() and ch.change_open


@pytest.mark.parametrize(
    ("target", "kind", "title", "idea", "needle"),
    [
        ("ghost", "feature", "t", "i", "no app 'ghost'"),
        ("APP", "dance", "t", "i", "a change is one of"),
        ("APP", "feature", "  ", "i", "needs a title"),
        ("APP", "feature", "t", "", "needs a title"),
    ],
)
def test_invalid_changes_are_refused(foreman, target, kind, title, idea, needle):
    app = shipped_app(foreman)
    with pytest.raises(FactoryError, match=needle):
        foreman.intake_change(app.slug if target == "APP" else target, title, idea, kind)


def test_an_app_that_is_not_shipped_cannot_be_changed(foreman):
    pending = foreman.run(foreman.intake("Pending", "an api", "poc"))
    with pytest.raises(FactoryError, match="not a shipped app"):
        foreman.intake_change(pending.slug, "t", "i")


def test_a_change_cannot_target_another_change(foreman):
    app = shipped_app(foreman)
    ch = foreman.intake_change(app.slug, "First", "x")
    with pytest.raises(FactoryError, match="not a shipped app"):
        foreman.intake_change(ch.slug, "Second", "y")


def test_one_open_change_per_app(foreman):
    app = shipped_app(foreman)
    first = foreman.intake_change(app.slug, "First", "x")
    with pytest.raises(FactoryError, match=f"already has an open change: {first.slug}"):
        foreman.intake_change(app.slug, "Second", "y")


# ------------------------------------------------------------------ the pipeline up to the plan


def test_the_change_spec_design_and_plan_describe_a_change_not_a_new_app(foreman):
    app = shipped_app(foreman)
    ch = to_plan_review(foreman, foreman.intake_change(app.slug, "Add a thing", "add a thing", "feature"))
    spec, design, tasks = (foreman.store.read(ch, n) for n in ("spec.md", "design.md", "tasks.md"))
    assert (ch.stage, ch.status) == ("plan_review", "waiting")  # poc, nothing needs IT: design review skipped
    assert f"a feature to the existing app `{app.slug}`" in spec and "BHV-1: no regression" in spec
    assert f"A feature to the EXISTING app `{app.slug}`" in design and "## 2. Existing stack (kept)" in design
    assert "FastAPI" in design and "Nothing to migrate away from." in design
    assert "already exists" in tasks and "never recreate it" in tasks


def test_the_design_lists_the_apps_existing_dependencies_as_pre_approved(foreman):
    app = shipped_app(foreman)
    ch = to_plan_review(foreman, foreman.intake_change(app.slug, "Add a thing", "add a thing"))
    design = foreman.store.read(ch, "design.md")
    for dep in ("fastapi", "pydantic", "uvicorn"):
        assert f"| {dep} |" in design
    assert "MUST allow these" in design


def test_a_migration_design_names_what_to_remove_without_failing_its_own_radar_check(foreman, tmp_path):
    app = shipped_app(foreman)
    foreman.radar, _ = radar_with(tmp_path, {"pydantic": "hold"})
    ch = to_plan_review(foreman, foreman.intake_change(app.slug, "Drop pydantic", "remove it", "migration"))
    design = foreman.store.read(ch, "design.md")
    assert ch.stage == "plan_review"  # the design stage did NOT block on the technology it is removing
    section = design.index("## 4. Tech radar constraints")
    start = design.index("<!-- radar:ignore -->", section)  # the migration list, not the title's own block
    end = design.index("<!-- /radar:ignore -->", start)
    assert "Pydantic v2 (hold)" in design[start:end]
    kept_sections = design.split("## 2.")[1].split("## 4.")[0]  # stack, approvals, dependencies
    assert "ydantic" not in kept_sections  # the removed technology is not listed as kept or pre-approved
    assert "Remove every technology listed under 'To migrate away from'" in design
    assert "migrating away from: pydantic" in " ".join(h["detail"] for h in ch.history)


def test_mvp_changes_still_get_it_design_review(foreman):
    app = shipped_app(foreman, "mvp")
    ch = foreman.run(foreman.intake_change(app.slug, "Add a thing", "add a thing"))
    ch = foreman.approve(ch, "business")
    assert (ch.stage, ch.status) == ("design_review", "waiting")


# ------------------------------------------------------------------ building on a branch


def test_the_offline_build_prepares_the_branch_and_leaves_the_base_untouched(foreman):
    app = shipped_app(foreman)
    base_tip = None
    ch = to_plan_review(foreman, foreman.intake_change(app.slug, "Add a thing", "add a thing"))
    ch = foreman.approve(ch, "owner")
    folder = foreman.cfg.apps_dir / app.slug
    assert current_branch(folder) == f"factory/{ch.slug}" and ch.base_branch == "main"
    base_tip = git(folder, "rev-parse", "main")
    assert git(folder, "rev-list", "--count", "main..HEAD") == "1"  # one commit: the change's own artifacts
    assert "add the spec, design and plan of the change" in git(folder, "log", "-1", "--format=%s")
    assert (folder / "work" / ch.slug / "spec.md").is_file() and ch.base_sha == base_tip
    assert (ch.stage, ch.status) == ("ship_review", "waiting")  # offline: the app is simply unchanged


def test_a_build_agent_edits_the_existing_app_and_its_work_lands_on_the_branch(foreman, tmp_path):
    app = shipped_app(foreman)
    foreman.radar, _ = radar_with(tmp_path, {"pydantic": "hold"})
    agent = ChangeAgent()
    foreman.runner = agent
    ch = to_plan_review(foreman, foreman.intake_change(app.slug, "Drop pydantic", "remove it", "migration"))
    ch = foreman.approve(ch, "owner")
    folder = foreman.cfg.apps_dir / app.slug
    assert (ch.stage, ch.status) == ("ship_review", "waiting"), ch.feedback
    build_prompt = next(p for p in agent.prompts if "implementer" in p)
    assert (
        "EXISTING app" in build_prompt
        and f"factory/{ch.slug}" in build_prompt
        and "pydantic" in build_prompt.lower()
    )
    assert "pydantic" not in (folder / "pyproject.toml").read_text(encoding="utf-8")
    assert "pydantic" in git(folder, "show", "main:pyproject.toml")  # the base still has it: nothing merged
    assert "the migration built by the build agent" in git(folder, "log", "--format=%s", "main..HEAD")
    assert porcelain(folder) == []


def test_a_change_that_leaves_a_forbidden_technology_in_place_is_blocked_by_the_gates(foreman, tmp_path):
    app = shipped_app(foreman)
    foreman.radar, _ = radar_with(tmp_path, {"pydantic": "hold"})
    foreman.runner = ChangeAgent(edit=False)  # an agent that does nothing
    ch = to_plan_review(foreman, foreman.intake_change(app.slug, "Drop pydantic", "remove it", "migration"))
    ch = foreman.approve(ch, "owner")
    assert (ch.stage, ch.status) == ("build", "blocked")
    assert "Pydantic" in ch.feedback and "hold" in ch.feedback


def test_the_target_app_disappearing_blocks_the_change_with_a_reason(foreman):
    import shutil

    app = shipped_app(foreman)
    ch = foreman.intake_change(app.slug, "Add a thing", "add a thing")
    shutil.rmtree(foreman.cfg.apps_dir / app.slug)
    ch = foreman.run(ch)
    assert ch.status == "blocked" and "folder" in ch.feedback and "missing" in ch.feedback


def test_an_it_rejection_reworks_on_the_same_branch(foreman, tmp_path):
    app = shipped_app(foreman)
    foreman.radar, _ = radar_with(tmp_path, {"pydantic": "hold"})
    agent = ChangeAgent()
    foreman.runner = agent
    ch = to_plan_review(foreman, foreman.intake_change(app.slug, "Drop pydantic", "remove it", "migration"))
    ch = foreman.approve(ch, "owner")
    ch = foreman.reject(ch, "it", "also mention it in the README", by="bob")
    ch = foreman.run(ch)
    folder = foreman.cfg.apps_dir / app.slug
    assert current_branch(folder) == f"factory/{ch.slug}" and ch.status == "waiting"
    build_prompts = [p for p in agent.prompts if "implementer" in p]
    assert len(build_prompts) == 2 and "also mention it in the README" in build_prompts[1]


# ------------------------------------------------------------------ merge: the human's act


def full_migration(foreman, tmp_path):
    app = shipped_app(foreman)
    foreman.radar, _ = radar_with(tmp_path, {"pydantic": "hold"})
    foreman.runner = ChangeAgent()
    ch = to_plan_review(foreman, foreman.intake_change(app.slug, "Drop pydantic", "remove it", "migration"))
    ch = foreman.approve(foreman.approve(ch, "owner"), "it", by="bob")
    return app, ch


def test_approval_does_not_merge_the_change_stays_on_its_branch_until_it_merges(foreman, tmp_path):
    app, ch = full_migration(foreman, tmp_path)
    folder = foreman.cfg.apps_dir / app.slug
    assert (ch.stage, ch.status, ch.merged) == ("shipped", "shipped", False)
    assert current_branch(folder) == f"factory/{ch.slug}" and "pydantic" in git(
        folder, "show", "main:pyproject.toml"
    )


def test_the_whole_story_migrate_approve_merge_and_the_app_is_compliant_again(foreman, tmp_path):
    app, ch = full_migration(foreman, tmp_path)
    folder = foreman.cfg.apps_dir / app.slug
    items = foreman.store.all()
    # before the merge: the app has a change in flight, so it is not judged
    assert scan_drift(items, foreman.radar, foreman.cfg.apps_dir, in_flight={app.slug}) == ([], [])
    merged = foreman.merge(ch, "it", "bob")
    assert merged.merged and current_branch(folder) == "main"
    assert "pydantic" not in (folder / "pyproject.toml").read_text(encoding="utf-8")
    drifted, clean = scan_drift(foreman.store.all(), foreman.radar, foreman.cfg.apps_dir)
    assert drifted == [] and [i.slug for i in clean] == [app.slug]  # drift is cured, for real
    assert not merged.change_open and any(h["event"] == "merged" for h in merged.history)
    assert foreman.intake_change(app.slug, "Next change", "another").change_open  # the app is free again


def test_only_it_merges_and_only_an_approved_change_once(foreman, tmp_path):
    app, ch = full_migration(foreman, tmp_path)
    for role in ("owner", "business"):
        with pytest.raises(FactoryError, match="only IT merges"):
            foreman.merge(ch, role)
    foreman.merge(ch, "it")
    with pytest.raises(FactoryError, match="already merged"):
        foreman.merge(ch, "it")
    with pytest.raises(FactoryError, match="only a change to an existing app is merged"):
        foreman.merge(foreman.store.load(app.slug), "it")


def test_an_unapproved_change_cannot_be_merged(foreman):
    app = shipped_app(foreman)
    ch = foreman.run(foreman.intake_change(app.slug, "Add a thing", "add a thing"))
    with pytest.raises(FactoryError, match="not approved yet"):
        foreman.merge(ch, "it")


def test_merge_refuses_when_the_base_moved_and_says_what_to_do(foreman, tmp_path):
    app, ch = full_migration(foreman, tmp_path)
    folder = foreman.cfg.apps_dir / app.slug
    git(folder, "switch", "-q", "main")
    (folder / "elsewhere.txt").write_text("a hotfix on main\n", encoding="utf-8")
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", "hotfix directly on main")
    git(folder, "switch", "-q", f"factory/{ch.slug}")
    with pytest.raises(FactoryError, match="has moved.*rebase"):
        foreman.merge(ch, "it")
    assert not foreman.store.load(ch.slug).merged and current_branch(folder) == f"factory/{ch.slug}"


# ------------------------------------------------------------------ abandon


def test_abandoning_frees_the_app_removes_the_branch_and_keeps_the_history(foreman):
    app = shipped_app(foreman)
    ch = foreman.approve(
        to_plan_review(foreman, foreman.intake_change(app.slug, "Add", "add a thing")), "owner"
    )
    folder = foreman.cfg.apps_dir / app.slug
    out = foreman.abandon(ch, "owner", "no longer needed", "antoine")
    assert out.status == "abandoned" and not out.change_open and current_branch(folder) == "main"
    assert git(folder, "branch", "--list", f"factory/{ch.slug}") == ""
    assert any(h["event"] == "abandoned" and "no longer needed" in h["detail"] for h in out.history)
    assert foreman.intake_change(app.slug, "Another", "idea").change_open  # not stuck anymore


@pytest.mark.parametrize(
    ("role", "reason", "needle"),
    [("business", "x", "only IT or the owner"), ("it", "  ", "needs a reason")],
)
def test_abandon_is_restricted_and_justified(foreman, role, reason, needle):
    app = shipped_app(foreman)
    ch = foreman.intake_change(app.slug, "Add", "add a thing")
    with pytest.raises(FactoryError, match=needle):
        foreman.abandon(ch, role, reason)


def test_a_merged_change_and_a_new_app_cannot_be_abandoned(foreman, tmp_path):
    app, ch = full_migration(foreman, tmp_path)
    foreman.merge(ch, "it")
    with pytest.raises(FactoryError, match="already merged"):
        foreman.abandon(ch, "it", "oops")
    with pytest.raises(FactoryError, match="only a change"):
        foreman.abandon(foreman.store.load(app.slug), "it", "oops")


def test_abandon_refuses_to_throw_away_uncommitted_work(foreman):
    app = shipped_app(foreman)
    ch = foreman.approve(
        to_plan_review(foreman, foreman.intake_change(app.slug, "Add", "add a thing")), "owner"
    )
    (foreman.cfg.apps_dir / app.slug / "precious.txt").write_text("unsaved", encoding="utf-8")
    with pytest.raises(FactoryError, match="uncommitted"):
        foreman.abandon(ch, "it", "changed my mind")
    assert foreman.store.load(ch.slug).status != "abandoned"


def test_only_an_app_is_promoted_never_a_change(foreman):
    app = shipped_app(foreman)
    ch = foreman.intake_change(app.slug, "Add", "add a thing")
    with pytest.raises(FactoryError, match="only an app is promoted"):
        foreman.promote(ch, "mvp", "it")


# ------------------------------------------------------------------ with Claudo (MVP and above)


def test_at_mvp_a_change_goes_through_claudo_on_the_apps_own_branch(foreman):
    app = shipped_app(foreman, "mvp")
    engine = ScriptedClaudo()
    foreman.runner, foreman.engine = ChangeAgent(), engine
    ch = foreman.run(foreman.intake_change(app.slug, "Add a thing", "add a thing"))
    ch = foreman.approve(foreman.approve(ch, "business"), "it")
    ch = foreman.approve(ch, "owner")
    folder = foreman.cfg.apps_dir / app.slug
    assert engine.runs and engine.runs[0]["project"] == folder  # Claudo works in the EXISTING app...
    assert (
        current_branch(folder) == f"factory/{ch.slug}" and ch.claudo_cp == "CP-1"
    )  # ...on the change branch


# ------------------------------------------------------------------ the CLI


@pytest.fixture
def cli_env(factory_root, monkeypatch):
    monkeypatch.setenv("AI_FACTORY_ROOT", str(factory_root))
    toml = factory_root / "factory.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace(', "tests"', "", 1), encoding="utf-8")
    return factory_root


def cli_ship(name="Orders"):
    slug = name.lower()
    assert main(["intake", name, "--idea", "an api", "--maturity", "poc"]) == 0
    for argv in (["run", slug], ["approve", slug, "--as", "business"], ["approve", slug, "--as", "owner"]):
        assert main(argv) == 0
    assert main(["approve", slug, "--as", "it"]) == 0
    return slug


def test_cli_change_run_merge_flow_and_messages(cli_env, capsys):
    app = cli_ship()
    capsys.readouterr()
    assert main(["change", app, "Add a thing", "--idea", "add a thing to orders", "--kind", "feature"]) == 0
    assert "Change created: add-a-thing  (feature of orders, maturity poc)" in capsys.readouterr().out
    for argv in (
        ["run", "add-a-thing"],
        ["approve", "add-a-thing", "--as", "business"],
        ["approve", "add-a-thing", "--as", "owner"],
        ["approve", "add-a-thing", "--as", "it"],
    ):
        assert main(argv) == 0
    out = capsys.readouterr().out
    assert "change: feature of orders, branch factory/add-a-thing (not merged yet)" in out
    assert "factory merge add-a-thing --as it" in out
    assert main(["merge", "add-a-thing", "--as", "it", "--by", "bob"]) == 0
    assert "Merged add-a-thing into main of orders (fast-forward)" in capsys.readouterr().out
    assert main(["show", "add-a-thing"]) == 0 and "(merged)" in capsys.readouterr().out


def test_cli_errors_are_clear_and_exit_2(cli_env, capsys):
    cli_ship()
    capsys.readouterr()
    assert main(["change", "ghost", "t", "--idea", "i"]) == 2
    assert "no app 'ghost'" in capsys.readouterr().err
    assert main(["merge", "orders", "--as", "it"]) == 2
    assert "only a change to an existing app is merged" in capsys.readouterr().err


def test_cli_abandon_and_drift_in_flight_message(cli_env, capsys):
    cli_ship()
    assert main(["change", "orders", "Add", "--idea", "x"]) == 0
    assert main(["run", "add"]) == 0
    capsys.readouterr()
    assert main(["drift"]) == 0  # a feature in flight is not a migration: nothing pending against the radar
    assert "1 app(s) with a change in flight" in capsys.readouterr().out
    assert main(["abandon", "add", "--as", "owner", "--reason", "not needed"]) == 0
    assert "orders is free for another change" in capsys.readouterr().out
    assert main(["change", "orders", "Again", "--idea", "y"]) == 0
