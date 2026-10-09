"""Locks, inbox identity, reviews that follow their artifact, honest publishing (audit A91, A106, A107,
A109, A110, A112, A119, A124, A125, A130, A131)."""

import os
import time

import pytest

from factory.foreman import FactoryError
from factory.workitem import ItemLocked, Store
from tests.test_inbox import IssueHost, issue

# ------------------------------------------------------------------ one command at a time


def test_a_second_command_on_a_locked_item_is_refused(tmp_path):
    store = Store(tmp_path)
    with store.lock("item-x"):
        with store.lock("item-x"):  # re-entrant within one command (approve -> run)
            pass
        other = Store(tmp_path)  # another process
        with pytest.raises(ItemLocked, match="another factory command is working on item-x"):
            with other.lock("item-x"):
                pass
    with Store(tmp_path).lock("item-x"):  # released at the end of the command
        pass


def test_a_lock_left_by_a_crashed_command_is_taken_over(tmp_path):
    lock = tmp_path / ".locks" / "item-x.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("pid 1\n", encoding="utf-8")
    old = time.time() - Store.STALE_LOCK_SECONDS - 60
    os.utime(lock, (old, old))
    with Store(tmp_path).lock("item-x"):
        assert lock.is_file()
    assert not lock.exists()


def test_the_foreman_locks_the_item_and_the_app_it_changes(foreman):
    item = foreman.intake("X", "an api", "poc")
    with Store(foreman.cfg.work_dir).lock(f"item-{item.slug}"):
        with pytest.raises(ItemLocked):
            foreman.run(item)
    with Store(foreman.cfg.work_dir).lock("app-orders"):
        with pytest.raises(ItemLocked):
            foreman.intake_change("./orders", "Add", "add a thing")


# ------------------------------------------------------------------ the inbox


def inbox_with(foreman, issues):
    foreman.host = IssueHost(issues)
    foreman.cfg.intake_repo, foreman.cfg.intake_label = "acme/ideas", "factory"
    return foreman


def with_id(raw, node):
    return {**raw, "id": node}


def test_a_renamed_intake_repository_does_not_reimport_its_issues(foreman):
    f = inbox_with(foreman, [with_id(issue(1, "Expense log"), "I_1")])
    [first], _ = f.inbox()
    moved = with_id(issue(1, "Expense log"), "I_1")
    moved["url"] = "https://github.com/acme/business-ideas/issues/1"
    f.host.issues = [moved]
    new, _ = f.inbox()
    assert new == [] and first.issue_id == "I_1"


def test_a_closed_issue_parks_its_item_once_and_gets_no_more_comments(foreman):
    f = inbox_with(foreman, [issue(1, "Expense log")])
    [item], _ = f.inbox()
    f.host.issues = []  # closed on GitHub
    f.host.comments.clear()
    f.inbox()
    parked = f.store.load(item.slug)
    assert (
        parked.issue_closed and parked.status == "blocked" and "was closed or unlabelled" in parked.feedback
    )
    assert f.host.comments == {}
    f.run(parked)  # a human resumes it: not parked again, still not commented
    f.inbox()
    assert f.store.load(item.slug).status != "blocked" and f.host.comments == {}


def test_a_gate_failure_comment_names_the_failed_gates(foreman):
    item = foreman.intake("X", "an api", "poc")
    item.status, item.feedback = "blocked", "gates failed:\n[tests] FAILED x\n\n[lint] E501"
    assert "- Blocked: gates failed: tests, lint" in foreman._issue_comment(item)
    item.feedback = "\nspec failed the structural lint"
    assert "- Blocked: spec failed the structural lint" in foreman._issue_comment(item)


def test_the_factory_name_is_used(foreman):
    foreman.cfg.name = "ACME Factory"
    assert foreman._issue_comment(foreman.intake("X", "an api", "poc")).startswith("Received by ACME Factory")


# ------------------------------------------------------------------ reviews follow their artifact


def test_an_artifact_edited_after_generation_is_linted_before_its_approval(foreman):
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    spec = foreman.store.read(item, "spec.md")
    foreman.store.write(item, "spec.md", spec.replace("| EVAL-", "| CHECK-"))  # the evals table is gone
    with pytest.raises(FactoryError, match="spec.md no longer passes the structural lint"):
        foreman.approve(item, "business")


def test_an_edited_plan_is_linted_before_its_approval(foreman):
    item = foreman.approve(foreman.run(foreman.intake("X", "an api", "poc")), "business")
    assert item.stage == "plan_review"
    plan = foreman.store.read(item, "tasks.md")
    foreman.store.write(item, "tasks.md", plan + "\n> Build it with Flask.\n")
    with pytest.raises(FactoryError, match="tasks.md no longer passes the plan lint"):
        foreman.approve(item, "owner")


def test_a_regenerated_artifact_drops_its_predecessors_reviews(foreman):
    item = foreman.intake("X", "an api", "poc")
    for name in ("spec-lint.md", "judge-spec.md"):
        foreman.store.write(item, name, "stale\n")
    item.judgements["spec"] = {"verdict": "pass"}
    foreman.run(item)
    assert not (foreman.store.dir(item.slug) / "judge-spec.md").exists()
    assert "spec" not in item.judgements


def test_the_capability_analyst_is_not_billed_for_a_change(foreman):
    calls = []
    foreman.runner = type("R", (), {"run": lambda self, *a, **k: calls.append(a)})()
    foreman.cfg.capability_analyst = True
    change = foreman.intake("X", "an api with a dashboard", "poc")
    change.kind = "feature"
    assert foreman._llm_capabilities(change, ["backend"]) == [] and calls == []
