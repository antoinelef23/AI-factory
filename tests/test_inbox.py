"""GitHub issue intake (ROADMAP P2-5): labelled issues become work items; progress is commented back."""

import json
from types import SimpleNamespace

import pytest

from factory import delivery
from factory.delivery import DeliveryError, GhCli
from factory.foreman import FactoryError
from factory.identity import RoleMap
from tests.test_identity import FakeIdentity


class IssueHost:
    """A git host with issues: list_issues serves `issues`, comments are recorded per issue url."""

    def __init__(self, issues):
        self.issues, self.comments = list(issues), {}

    def list_issues(self, repo, label):
        self.asked = (repo, label)
        return [dict(i) for i in self.issues]

    def comment_issue(self, url, body):
        self.comments.setdefault(url, []).append(body)


def issue(number, title="Expense log", body="Employees record expenses.", author="alice", labels=()):
    return {
        "number": number,
        "title": title,
        "body": body,
        "author": author,
        "url": f"https://github.com/acme/ideas/issues/{number}",
        "labels": ["factory", *labels],
    }


@pytest.fixture
def inbox(foreman):
    def make(issues):
        foreman.host = IssueHost(issues)
        foreman.cfg.intake_repo, foreman.cfg.intake_label = "acme/ideas", "factory"
        return foreman

    return make


def test_labelled_issues_become_work_items_and_are_told_so(inbox):
    f = inbox(
        [issue(1), issue(2, "Visitor badges", "Print a badge for each visitor.", labels=("maturity:mvp",))]
    )
    new, skipped = f.inbox()
    assert [(i.title, i.maturity, i.requester) for i in new] == [
        ("Expense log", "poc", "alice"),
        ("Visitor badges", "mvp", "alice"),
    ]
    assert f.host.asked == ("acme/ideas", "factory") and skipped == []
    first = new[0]
    assert first.idea == "Employees record expenses." and first.issue_url.endswith("/issues/1")
    [comment] = f.host.comments[first.issue_url]
    assert "Received by AI Software Factory as work item `expense-log`" in comment  # [factory] name
    assert "triage" in comment and first.issue_reported == "triage/active"


def test_an_issue_is_imported_once(inbox):
    f = inbox([issue(1)])
    f.inbox()
    new, _ = f.inbox()
    assert new == [] and len(f.store.all()) == 1
    assert (
        len(f.host.comments["https://github.com/acme/ideas/issues/1"]) == 1
    )  # nothing changed: no new comment


def test_progress_is_commented_when_the_stage_changes_and_only_then(inbox):
    f = inbox([issue(1)])
    [item] = f.inbox()[0]
    item = f.run(item)  # spec written: waiting for the business
    assert f.report_issues() == 1
    latest = f.host.comments[item.issue_url][-1]
    assert "spec_review" in latest and "Waiting for: the **business** decision" in latest
    assert f.report_issues() == 0


def test_a_maturity_line_in_the_body_is_read(inbox):
    f = inbox([issue(1, body="We need this live.\nMaturity: MVP\n")])
    assert f.inbox()[0][0].maturity == "mvp"


def test_with_identity_only_business_holders_can_file_ideas(inbox):
    f = inbox([issue(1, author="alice"), issue(2, title="Sneaky", author="mallory")])
    f.identity, f.roles = FakeIdentity("bob"), RoleMap({"business": ["alice"], "it": ["bob"], "owner": []})
    new, skipped = f.inbox()
    assert [i.title for i in new] == ["Expense log"]
    assert skipped == ["#2 'Sneaky': author mallory does not hold the business role in roles.toml"]
    assert "https://github.com/acme/ideas/issues/2" not in f.host.comments  # no comment on a refused issue


def test_intake_needs_a_repository_and_delivery(foreman):
    with pytest.raises(FactoryError, match="delivery is off"):
        foreman.inbox()
    foreman.host = IssueHost([])
    with pytest.raises(FactoryError, match=r"\[intake\] repo"):
        foreman.inbox()


def test_a_delivered_item_reports_its_repository(inbox):
    f = inbox([issue(1)])
    [item] = f.inbox()[0]
    item.repo_url, item.status, item.stage = (
        "https://github.com/acme/app-expense-log.git",
        "shipped",
        "shipped",
    )
    f.store.save(item)
    f.report_issues()
    assert "https://github.com/acme/app-expense-log (private)" in f.host.comments[item.issue_url][-1]


# ------------------------------------------------------------------ the gh adapter


def test_gh_lists_labelled_open_issues(monkeypatch):
    calls = []
    answer = [
        {
            "id": "I_kwDOAbc123",
            "number": 7,
            "title": "T",
            "body": None,
            "author": {"login": "alice"},
            "url": "https://github.com/a/b/issues/7",
            "labels": [{"name": "factory"}, {"name": "maturity:mvp"}],
        }
    ]

    def fake(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps(answer), stderr="")

    monkeypatch.setattr(delivery.subprocess, "run", fake)
    [got] = GhCli().list_issues("a/b", "factory")
    assert got == {
        "id": "I_kwDOAbc123",  # the stable identity the inbox dedupes on (A106)
        "number": 7,
        "title": "T",
        "body": "",
        "author": "alice",
        "url": "https://github.com/a/b/issues/7",
        "labels": ["factory", "maturity:mvp"],
    }
    argv = calls[0]
    assert argv[1:3] == ["issue", "list"] and "--label" in argv and "open" in argv


def test_gh_comments_with_the_body_on_stdin(monkeypatch):
    seen = {}

    def fake(argv, **kw):
        seen["argv"], seen["input"] = argv, kw.get("input")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(delivery.subprocess, "run", fake)
    GhCli().comment_issue("https://github.com/a/b/issues/7", "hello")
    assert seen["argv"][1:3] == ["issue", "comment"] and seen["input"] == "hello"


def test_gh_issue_list_garbage_is_an_error(monkeypatch):
    monkeypatch.setattr(
        delivery.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="nope", stderr="")
    )
    with pytest.raises(DeliveryError, match="unreadable"):
        GhCli().list_issues("a/b", "factory")
