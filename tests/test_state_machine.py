"""Terminal states stay terminal and sibling commands share their guards (H4.2: audit A27, A28, A33-A35,
A42-A44, A111, A113-A115, A25/A132)."""

from datetime import date

import pytest

from factory.foreman import FactoryError
from tests.test_change_flow import full_migration, git, shipped_app, to_plan_review


def abandoned_change(foreman):
    app = shipped_app(foreman)
    ch = foreman.run(foreman.intake_change(app.slug, "Add", "add a thing"))
    return app, foreman.abandon(ch, "it", "not needed")


@pytest.mark.parametrize(
    "act",
    [
        lambda f, ch: f.run(ch),
        lambda f, ch: f.approve(ch, "business"),
        lambda f, ch: f.reject(ch, "business", "no"),
        lambda f, ch: f.allow(ch, "flask", "it"),
        lambda f, ch: f.merge(ch, "it"),
        lambda f, ch: f.publish(ch, "it"),
    ],
)
def test_an_abandoned_change_cannot_be_revived(foreman, act):
    app, ch = abandoned_change(foreman)
    with pytest.raises(FactoryError, match="was abandoned"):
        act(foreman, ch)
    assert foreman.store.load(ch.slug).status == "abandoned"


def test_a_change_merged_on_the_host_is_never_abandoned_even_on_a_second_try(foreman):
    app, ch = abandoned_change(foreman)
    ch.status, ch.pr_state = "shipped", "MERGED"  # the first abandon saw MERGED on the host and saved it
    with pytest.raises(FactoryError, match="already merged.*factory sync"):
        foreman.abandon(ch, "it", "again")


def test_merge_takes_only_the_approved_commit(foreman, tmp_path):
    app, ch = full_migration(foreman, tmp_path)
    folder = foreman.cfg.apps_dir / app.slug
    (folder / "late.txt").write_text("after the approval\n", encoding="utf-8")
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", "never approved")
    with pytest.raises(FactoryError, match="only the approved commit is merged"):
        foreman.merge(ch, "it")
    assert not foreman.store.load(ch.slug).merged


def test_a_merged_changes_exceptions_become_the_apps(foreman, tmp_path):
    app, ch = full_migration(foreman, tmp_path)
    ch = foreman.allow(ch, "some-new-lib", "it", expires=date(2099, 1, 1), reason="needed for the change")
    foreman.merge(ch, "it")
    target = foreman.store.load(app.slug)
    assert "some-new-lib" in target.it_exceptions
    assert target.exception_terms["some-new-lib"]["expires"] == "2099-01-01"


def test_promote_is_refused_while_a_change_is_open(foreman):
    app = shipped_app(foreman)
    foreman.intake_change(app.slug, "Add", "add a thing")
    with pytest.raises(FactoryError, match="has an open change"):
        foreman.promote(foreman.store.load(app.slug), "mvp", "it")


def test_promote_starts_the_next_rung_from_a_clean_build_state(foreman):
    app = shipped_app(foreman)
    state = foreman.app_dir(app) / "work" / app.slug / ".runs" / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text('{"T1": "done"}', encoding="utf-8")
    app.ship_acks = [{"kind": "scope_drift", "detail": "x"}]
    app.claudo_cp, app.built_with_claudo, app.gated_sha = "CP-1", True, "abc"
    promoted = foreman.promote(app, "mvp", "it")
    assert (promoted.claudo_cp, promoted.built_with_claudo) == ("", False)
    assert not any(a["kind"] == "scope_drift" for a in promoted.ship_acks)
    assert not state.exists()


def test_a_shipped_change_stays_shipped_when_its_app_is_promoted(foreman, tmp_path):
    app, ch = full_migration(foreman, tmp_path)
    foreman.merge(ch, "it")
    foreman.promote(foreman.store.load(app.slug), "mvp", "it")  # the app is no longer "shipped" for a while
    again = foreman.run(foreman.store.load(ch.slug))
    assert (again.stage, again.status) == ("shipped", "shipped")


def test_a_run_that_exhausts_its_step_budget_is_blocked_not_active(foreman):
    item = foreman.intake("X", "an api", "poc")
    item = foreman.run(item, max_steps=1)
    assert item.status == "blocked" and "stopped after 1 automatic steps" in item.feedback


def test_one_open_change_per_app_however_the_target_is_spelled(foreman):
    app = shipped_app(foreman)
    foreman.intake_change(app.slug, "First", "a first change")
    for spelling in (f"./{app.slug}", f"{app.slug}/", f"apps\\{app.slug}"):
        with pytest.raises(FactoryError, match="already has an open change"):
            foreman.intake_change(spelling, "Second", "a second change")


def test_the_spelled_target_is_stored_canonically(foreman):
    app = shipped_app(foreman)
    ch = foreman.intake_change(f"./{app.slug}/", "Add", "add a thing")
    assert ch.target == app.slug
    assert to_plan_review(foreman, ch).target == app.slug
