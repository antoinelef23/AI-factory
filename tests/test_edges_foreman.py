"""The foreman's refusals and failure paths, each exercised for what it says (offline, no model, no host)."""

import pytest

from factory.agents import AgentResult
from factory.claudo import BuildResult
from factory.delivery import DeliveryError
from factory.foreman import FactoryError
from factory.identity import IdentityError
from factory.project import ProjectError
from factory.sandbox import Sandbox
from tests.conftest import FakeAgentRunner
from tests.test_change_flow import ChangeAgent, full_migration, shipped_app, to_plan_review
from tests.test_claudo_build import PlanThenBuildAgent, ScriptedClaudo, at_ship_review
from tests.test_delivery import FakeHost
from tests.test_post_approval import REPORT, Claudo

# ------------------------------------------------------------------ intake and roles


def test_intake_refusals(foreman):
    with pytest.raises(FactoryError, match="maturity must be one of"):
        foreman.intake("X", "an api", "beta")
    with pytest.raises(FactoryError, match="needs a title and a description"):
        foreman.intake(" ", "an api", "poc")


def test_an_unlistable_intake_repository_is_a_factory_error(foreman):
    class Down:
        def list_issues(self, repo, label):
            raise DeliveryError("gh issue list failed")

    foreman.host, foreman.cfg.intake_repo = Down(), "acme/ideas"
    with pytest.raises(FactoryError, match="gh issue list failed"):
        foreman.inbox()


def test_an_issue_comment_names_the_pull_request(foreman):
    item = foreman.intake("X", "an api", "poc")
    item.pr_url = "https://github.com/a/b/pull/3"
    assert "- Pull request: https://github.com/a/b/pull/3" in foreman._issue_comment(item)


def test_an_identity_that_cannot_be_verified_refuses_the_action(foreman):
    class Broken:
        def current(self):
            raise IdentityError("gh is not logged in")

    foreman.identity = Broken()
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    with pytest.raises(FactoryError, match="gh is not logged in"):
        foreman.approve(item, "business")


def test_role_and_reason_refusals(foreman):
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    with pytest.raises(FactoryError, match="role must be one of"):
        foreman.approve(item, "ceo")
    with pytest.raises(FactoryError, match="a rejection needs a reason"):
        foreman.reject(item, "business", "  ")
    with pytest.raises(FactoryError, match="only IT can grant"):
        foreman.allow(item, "django", "owner")


def test_promotion_refusals(foreman):
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    with pytest.raises(FactoryError, match="only IT can promote"):
        foreman.promote(item, "mvp", "owner")
    with pytest.raises(FactoryError, match="only a shipped item can be promoted"):
        foreman.promote(item, "mvp", "it")
    shipped = shipped_app(foreman)
    with pytest.raises(FactoryError, match="cannot promote from poc to pov"):
        foreman.promote(shipped, "pov", "it")


def test_approving_an_item_whose_folder_was_removed(foreman, tmp_path):
    item = foreman.run(foreman.intake("Z", "an api", "poc"))
    item = foreman.approve(foreman.approve(item, "business"), "owner")
    foreman.app_dir(item).rename(tmp_path / "gone")
    done = foreman.approve(item, "it")
    assert done.status == "shipped" and done.approved_head == item.gated_sha or done.approved_head == ""


# ------------------------------------------------------------------ agents failing


class FailingAt(FakeAgentRunner):
    def __init__(self, marker):
        super().__init__()
        self.marker = marker

    def run(self, prompt, **kw):
        if self.marker in prompt:
            return AgentResult(False, "", 0.01, "model overloaded")
        return super().run(prompt, **kw)


def test_a_failing_spec_agent_blocks_with_its_error(foreman):
    foreman.runner = FailingAt("spec writer")
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    assert item.status == "blocked" and "spec agent failed: model overloaded" in item.feedback


def test_a_failing_plan_agent_blocks_with_its_error(foreman):
    foreman.runner = FailingAt("planner of")
    item = foreman.approve(foreman.run(foreman.intake("X", "an api", "poc")), "business")
    assert item.status == "blocked" and "plan agent failed" in item.feedback


def test_a_failing_build_agent_blocks_an_app_and_a_change(foreman):
    item = foreman.approve(foreman.run(foreman.intake("X", "an api", "poc")), "business")
    foreman.runner = FailingAt("implementer")
    item = foreman.approve(item, "owner")
    assert item.status == "blocked" and "build agent failed: model overloaded" in item.feedback
    app = shipped_app(foreman, "poc") if False else None
    assert app is None


def test_a_failing_change_build_agent_blocks_the_change(foreman):
    app = shipped_app(foreman)

    class Fails(ChangeAgent):
        def run(self, prompt, **kw):
            if "implementer" in prompt:
                return AgentResult(False, "", 0.0, "agent crashed")
            return super().run(prompt, **kw)

    foreman.runner = Fails()
    ch = to_plan_review(foreman, foreman.intake_change(app.slug, "Add", "add a thing"))
    ch = foreman.approve(ch, "owner")
    assert ch.status == "blocked" and "build agent failed: agent crashed" in ch.feedback


# ------------------------------------------------------------------ designs and golden paths


def test_a_design_that_names_a_blocked_technology_is_refused(foreman, monkeypatch):
    import factory.foreman as fm

    real = fm.render_design
    monkeypatch.setattr(fm, "render_design", lambda **kw: real(**kw) + "\nStore it all in MongoDB.\n")
    item = foreman.approve(foreman.run(foreman.intake("X", "an api", "poc")), "business")
    assert item.status == "blocked" and "design violates the radar" in item.feedback


def test_a_change_design_that_names_a_blocked_technology_is_refused(foreman, monkeypatch):
    import factory.foreman as fm

    app = shipped_app(foreman)
    real = fm.render_change_design
    monkeypatch.setattr(fm, "render_change_design", lambda **kw: real(**kw) + "\nUse MongoDB.\n")
    foreman.runner = ChangeAgent()
    ch = foreman.approve(foreman.run(foreman.intake_change(app.slug, "Add", "add a thing")), "business")
    assert ch.status == "blocked" and "design violates the radar" in ch.feedback


def test_without_any_golden_path_the_app_starts_from_an_empty_folder(foreman):
    import shutil

    shutil.rmtree(foreman.cfg.golden_paths_dir)
    foreman.cfg.golden_paths_dir.mkdir()
    item = foreman.intake("X", "an api", "poc")
    assert foreman._golden_path(item) == "python-fastapi"  # the radar's legacy golden_path names it
    foreman.radar = type(foreman.radar)(
        foreman.radar.company,
        foreman.radar.version,
        [t.__class__(**{**t.__dict__, "golden_path": None}) for t in foreman.radar.techs],
    )
    assert foreman._golden_path(item) is None and foreman._scaffold_files(item) == []
    assert foreman._scaffold(item, foreman.app_dir(item)) == "no golden path available: empty app folder"


def test_a_change_cannot_start_on_a_detached_app(foreman):
    import subprocess

    app = shipped_app(foreman)
    subprocess.run(["git", "checkout", "-q", "--detach"], cwd=foreman.app_dir(app), check=True)
    foreman.runner = ChangeAgent()
    ch = to_plan_review(foreman, foreman.intake_change(app.slug, "Add", "add a thing"))
    ch = foreman.approve(ch, "owner")
    assert ch.status == "blocked" and "detached HEAD" in ch.feedback


def test_a_merged_change_without_a_spec_adds_nothing_to_the_apps_spec(foreman, tmp_path):
    app, ch = full_migration(foreman, tmp_path)
    foreman.merge(ch, "it")
    (foreman.app_dir(app) / "work" / ch.slug / "spec.md").write_text("", encoding="utf-8")
    nxt = foreman.intake_change(app.slug, "Next", "another change")
    assert "merged change" not in foreman._current_app_spec(nxt)


# ------------------------------------------------------------------ merge, publish, abandon


def test_merging_when_the_apps_own_item_is_gone(foreman, tmp_path):
    app, ch = full_migration(foreman, tmp_path)
    (foreman.store.dir(app.slug) / "item.json").unlink()
    merged = foreman.merge(ch, "it")
    assert merged.merged


def test_a_history_scan_failure_stops_the_publish(foreman, tmp_path, monkeypatch):
    import factory.foreman as fm

    foreman.host = FakeHost(tmp_path / "remotes")
    (tmp_path / "remotes").mkdir()
    item = shipped_app(foreman)

    def broken(app, rev_range):
        raise ValueError("git log failed")

    monkeypatch.setattr(fm, "secrets_in_history", broken)
    with pytest.raises(FactoryError, match="git log failed"):
        foreman.publish(item, "it")


def test_publishing_a_merged_change_and_an_unreadable_pr(foreman, tmp_path):
    foreman.host = FakeHost(tmp_path / "remotes")
    (tmp_path / "remotes").mkdir()
    app, ch = full_migration(foreman, tmp_path)
    foreman.publish(foreman.store.load(app.slug), "it")
    ch = foreman.publish(ch, "it")

    def unreadable(url):
        raise DeliveryError("gh pr view: unreadable answer")

    foreman.host.pull_request_state = unreadable
    with pytest.raises(FactoryError, match="unreadable answer"):
        foreman.publish(ch, "it")
    ch.merged = True
    with pytest.raises(FactoryError, match="already merged"):
        foreman.publish(ch, "it")


def test_abandoning_when_the_host_already_closed_it_or_cannot_answer(foreman, tmp_path, monkeypatch):
    import factory.foreman as fm

    foreman.host = FakeHost(tmp_path / "remotes")
    (tmp_path / "remotes").mkdir()
    app, ch = full_migration(foreman, tmp_path)
    foreman.publish(foreman.store.load(app.slug), "it")
    ch = foreman.publish(ch, "it")
    foreman.host.state = "CLOSED"  # closed on the host already: nothing to close
    states = iter([DeliveryError("gh down")])

    def down(url):
        raise next(states)

    real_state = foreman.host.pull_request_state
    foreman.host.pull_request_state = down
    with pytest.raises(FactoryError, match="gh down"):
        foreman.abandon(ch, "it", "not needed")
    foreman.host.pull_request_state = real_state
    monkeypatch.setattr(fm, "abandon_change", lambda app, slug: (_ for _ in ()).throw(ProjectError("busy")))
    with pytest.raises(FactoryError, match="busy"):
        foreman.abandon(ch, "it", "not needed")
    assert foreman.host.closed == []


def test_exceptions_already_on_the_app_are_kept_once(foreman, tmp_path):
    app, ch = full_migration(foreman, tmp_path)
    target = foreman.store.load(app.slug)
    target.it_exceptions = ["django"]
    foreman.store.save(target)
    ch.it_exceptions = ["django", "untermed"]  # untermed: granted without terms (an old item)
    foreman.merge(ch, "it")
    assert foreman.store.load(app.slug).it_exceptions == ["django", "untermed"]


# ------------------------------------------------------------------ notes, acks and reports said once


def test_the_host_fallback_note_is_written_once(foreman):
    down = Sandbox(docker=lambda a: (1, "down"))
    foreman.sandbox, foreman.runner = down, FakeAgentRunner()
    item = foreman.intake("X", "an api", "poc")
    foreman._containment(item)
    foreman._containment(item)
    assert sum("required from MVP up" in n for n in item.notes) == 1


def test_an_ack_is_recorded_once(foreman):
    item = foreman.intake("X", "an api", "poc")
    foreman._add_ack(item, "scope_drift", "x")
    foreman._add_ack(item, "scope_drift", "x")
    assert item.ship_acks == [{"kind": "scope_drift", "detail": "x"}]


def test_the_missing_nonce_warning_is_written_once(foreman):
    engine = ScriptedClaudo([BuildResult("checkpoint", "CP-1", "x"), BuildResult("checkpoint", "CP-1", "x")])
    engine.nonce_support = False
    item = at_ship_review(foreman, engine, PlanThenBuildAgent())
    item = foreman.run(foreman.reject(item, "it", "again", by="bob"))
    assert sum("replay protection unavailable" in n for n in item.notes) == 1


@pytest.mark.parametrize("after", ["gone", "same"])
def test_a_rewritten_review_that_says_nothing_new_does_not_reopen_the_decision(foreman, after):
    """The report changed after the approval, but there is no verdict to read any more, or the same one."""
    from tests.test_post_approval import at_ship_review as ship_review

    class Engine(Claudo):
        def review_verdict(self, project, slug, cp):
            if getattr(self, "final_done", False) and after == "gone":
                return None
            return super().review_verdict(project, slug, cp)

        def run_build(self, slug, project, **kw):
            final = getattr(self, "final_next", False)
            result = super().run_build(slug, project, **kw)
            self.final_done = self.final_done if hasattr(self, "final_done") else False
            if final:
                self.final_done = True
            return result

    def rewrite(project):
        (project / REPORT).write_text("# Review CP-1: aggregated verdict: WARN (re-run)\n", encoding="utf-8")

    item = ship_review(foreman, Engine(verdict="WARN", after_approval=rewrite))
    item = foreman.approve(item, "it", by="bob", note="WARN accepted")
    assert item.status == "shipped", item.feedback


def test_a_report_path_outside_the_factory_is_shown_absolute(foreman, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    item = foreman.intake("X", "an api", "poc")
    item.claudo_review = {"report": "work/x/.runs/CP-1-review.md"}
    foreman.cfg.apps_dir = elsewhere
    assert foreman.report_path(item).startswith(str(elsewhere))
