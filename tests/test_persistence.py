"""Nothing done or paid for is lost when a step fails (H4.3: audit A26, A29, A30, A36, A37, A62, A63, A89,
A108, A174 and templates.py:187)."""

import json
import subprocess

import pytest

from factory.agents import AgentResult
from factory.claudo import EngineError
from factory.cli import main
from factory.foreman import FactoryError
from factory.project import ProjectError, porcelain, prepare_project
from factory.workitem import ItemNotFound, Store, WorkItem
from tests.test_plan_lint_loop import BAD, GOOD, FakeEngine, Scripted

# ------------------------------------------------------------------ a failing step saves the item first


class TimingOutEngine(FakeEngine):
    def lint_plan(self, slug, **kw):
        raise EngineError("plan-lint timed out after 60s")


def test_an_engine_timeout_mid_run_keeps_the_approval_and_the_cost(foreman):
    foreman.runner, foreman.engine = Scripted([GOOD]), TimingOutEngine()
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    with pytest.raises(FactoryError, match="blocked: EngineError: plan-lint timed out"):
        foreman.approve(item, "business", by="alice")
    saved = foreman.store.load(item.slug)
    assert saved.status == "blocked" and "plan-lint timed out" in saved.feedback
    assert [a["role"] for a in saved.approvals] == ["business"] and saved.cost_usd > 0


def test_a_bug_inside_a_step_keeps_its_traceback_but_the_item_is_saved(foreman, monkeypatch):
    item = foreman.run(foreman.intake("X", "an api", "poc"))

    def broken(_item):
        raise KeyError("a real bug")

    monkeypatch.setattr(foreman, "_do_design", broken)
    with pytest.raises(KeyError, match="a real bug"):
        foreman.approve(item, "business")
    assert foreman.store.load(item.slug).status == "blocked"


def test_a_policy_refusal_inside_a_step_is_raised_as_is_and_saved(foreman, monkeypatch):
    item = foreman.run(foreman.intake("X", "an api", "poc"))

    def refuse(_item):
        raise FactoryError("IT must fix the golden path")

    monkeypatch.setattr(foreman, "_do_design", refuse)
    with pytest.raises(FactoryError, match="IT must fix the golden path"):
        foreman.approve(item, "business")
    assert "IT must fix the golden path" in foreman.store.load(item.slug).feedback


def test_a_git_failure_in_claudos_final_step_blocks_the_ship_and_keeps_the_cost(foreman, monkeypatch):
    from factory.claudo import BuildResult
    from tests.test_claudo_build import PlanThenBuildAgent, ScriptedClaudo, at_ship_review

    engine = ScriptedClaudo(
        [BuildResult("checkpoint", "CP-1", "T1 done\n⏸  CP-1 — waiting for x"), BuildResult("done", log="ok")]
    )
    item = at_ship_review(foreman, engine, PlanThenBuildAgent())
    engine.journal_total = 1.5

    def git_down(*a, **k):
        raise ProjectError("git diff timed out")

    monkeypatch.setattr("factory.foreman.post_approval_changes", git_down)
    item = foreman.approve(item, "it", by="bob")
    saved = foreman.store.load(item.slug)
    assert saved.status == "blocked" and "git diff timed out" in saved.feedback and saved.cost_usd >= 1.5


# ------------------------------------------------------------------ the store


def test_item_json_is_replaced_atomically(tmp_path):
    store = Store(tmp_path)
    item = WorkItem(slug="x", title="X", idea="i", maturity="poc")
    store.save(item)
    store.save(item)
    assert [p.name for p in (tmp_path / "x").iterdir()] == ["item.json"]


def test_one_corrupt_item_does_not_break_the_others(tmp_path):
    store = Store(tmp_path)
    store.save(WorkItem(slug="good", title="G", idea="i", maturity="poc"))
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "item.json").write_text("{ half written", encoding="utf-8")
    (tmp_path / "odd").mkdir()
    (tmp_path / "odd" / "item.json").write_text(json.dumps(["not", "an", "item"]), encoding="utf-8")
    assert [i.slug for i in store.all()] == ["good"]
    assert [u.split(":")[0] for u in store.unreadable] == ["bad", "odd"]


def test_a_missing_item_is_its_own_error_and_the_cli_reports_it(factory_root, monkeypatch, capsys):
    with pytest.raises(ItemNotFound):
        Store(factory_root / "work").load("nope")
    monkeypatch.setenv("AI_FACTORY_ROOT", str(factory_root))
    assert main(["show", "nope"]) == 2
    assert "no work item 'nope'" in capsys.readouterr().err


def test_an_internal_key_error_is_not_disguised_as_a_missing_item(factory_root, monkeypatch):
    import factory.cli as cli

    monkeypatch.setenv("AI_FACTORY_ROOT", str(factory_root))

    def bug(args):
        raise KeyError("stage")

    monkeypatch.setattr(cli, "cmd_board", bug)
    with pytest.raises(KeyError):
        main(["board"])


# ------------------------------------------------------------------ git status parsing


def test_porcelain_reads_non_ascii_names_and_renames(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "old.txt").write_text("x\n", encoding="utf-8")
    prepare_project(app)
    (app / "docs").mkdir()
    (app / "docs" / "équipe.md").write_text("é\n", encoding="utf-8")
    subprocess.run(["git", "mv", "old.txt", "new name.txt"], cwd=app, check=True, capture_output=True)
    assert sorted(porcelain(app)) == ["docs/équipe.md", "new name.txt"]


# ------------------------------------------------------------------ what the next attempt is told


class NeverLintsSpec:
    name = "claude"

    def __init__(self):
        self.prompts = []

    def run(self, prompt, **kw):
        self.prompts.append(prompt)
        return AgentResult(True, "# not a spec", 0.01)


def test_the_rejection_reason_survives_the_lint_failure_that_follows(foreman):
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    runner = NeverLintsSpec()
    foreman.runner = runner
    item = foreman.run(foreman.reject(item, "business", "drop the export feature"))
    assert item.status == "blocked"  # the respec never passed the lint
    runner.prompts.clear()
    foreman.run(item)  # a human re-runs: the reason is still in the prompt, next to the lint errors
    assert "drop the export feature" in runner.prompts[0]
    assert "The last automatic check failed" in runner.prompts[0]


def test_a_resolved_failure_is_not_fed_to_a_later_stage(foreman):
    item = foreman.intake("X", "an api", "poc")
    item.feedback = "spec failed the structural lint: old news"
    item = foreman.run(item)  # the spec passes this time
    assert item.feedback == "" and item.stage == "spec_review"


def test_approval_clears_the_rejection(foreman):
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    item = foreman.run(foreman.reject(item, "business", "more detail"))
    assert item.rejection == "more detail" and item.stage == "spec_review"  # survived the respec
    item = foreman.approve(item, "business")
    assert item.rejection == ""


SCOPE_WARNED = (
    "---\ntype: tasks\nstatus: proposed\n---\n### T1 — Route\n- **depends_on :** []\n"
    "- **files_touched :** `tests/test_x.py`\n- **prompt :**\n  > Update `app/main.py` to add the route.\n"
)


def test_a_lint_clean_plan_is_kept_when_the_scope_reprompt_breaks_it(foreman):
    foreman.cfg.plan_lint_retries = 1
    runner = Scripted([SCOPE_WARNED, BAD])  # clean but scope-warned, then broken by the re-prompt
    foreman.runner, foreman.engine = runner, FakeEngine()
    item = foreman.approve(foreman.run(foreman.intake("X", "an api", "poc")), "business")
    assert (item.stage, item.status) == ("plan_review", "waiting")
    assert "Update `app/main.py`" in foreman.store.read(item, "tasks.md")


# ------------------------------------------------------------------ the inbox


def test_one_failing_issue_comment_does_not_stop_the_inbox(foreman):
    from factory.delivery import DeliveryError
    from tests.test_inbox import IssueHost, issue

    class DeletedFirst(IssueHost):
        def comment_issue(self, url, body):
            if url.endswith("/issues/1"):
                raise DeliveryError("gh issue comment failed: issue not found")
            super().comment_issue(url, body)

    foreman.host = DeletedFirst([issue(1, "First idea"), issue(2, "Second idea")])
    foreman.cfg.intake_repo, foreman.cfg.intake_label = "acme/ideas", "factory"
    new, skipped = foreman.inbox()
    assert [i.title for i in new] == ["First idea", "Second idea"]
    assert any("could not comment on https://github.com/acme/ideas/issues/1" in s for s in skipped)
    assert list(foreman.host.comments) == ["https://github.com/acme/ideas/issues/2"]
    assert all(i.issue_url for i in new)


def test_the_board_names_an_unreadable_item(factory_root, monkeypatch, capsys):
    monkeypatch.setenv("AI_FACTORY_ROOT", str(factory_root))
    (factory_root / "work" / "bad").mkdir(parents=True)
    (factory_root / "work" / "bad" / "item.json").write_text("{", encoding="utf-8")
    assert main(["board"]) == 0
    assert "UNREADABLE bad:" in capsys.readouterr().err
