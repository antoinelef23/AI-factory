"""The CLI commands the walkthrough tests do not reach: inbox, reject, promote, export, demo failures."""

import io
import sys

import pytest

import factory.cli as cli
from factory.cli import main
from factory.workitem import Store
from tests.test_inbox import IssueHost, issue


@pytest.fixture
def cli_root(factory_root, monkeypatch):
    """Offline CLI with the radar/secrets/immutable gates (the tests gate would run real uv)."""
    monkeypatch.setenv("AI_FACTORY_ROOT", str(factory_root))
    toml = factory_root / "factory.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace(', "tests"', "", 1), encoding="utf-8")
    return factory_root


def ship(slug="orders"):
    assert main(["intake", "Orders", "--idea", "an api", "--maturity", "poc"]) == 0
    for argv in (["run", slug], ["approve", slug, "--as", "business"], ["approve", slug, "--as", "owner"]):
        assert main(argv) == 0
    assert main(["approve", slug, "--as", "it"]) == 0


def test_inbox_from_the_command_line(cli_root, monkeypatch, capsys):
    toml = cli_root / "factory.toml"
    text = toml.read_text(encoding="utf-8")
    text = text.replace('provider = "none"\nowner = ""', 'provider = "github"\nowner = ""', 1)
    toml.write_text(text.replace('repo = ""', 'repo = "acme/ideas"', 1), encoding="utf-8")
    host = IssueHost([issue(1, "Expense log"), issue(2, " ")])
    monkeypatch.setattr(cli, "GhCli", lambda: host)
    assert main(["inbox"]) == 0
    out = capsys.readouterr().out
    assert "New item expense-log [poc]" in out and "skipped #2" in out
    host.issues = [issue(1, "Expense log")]
    assert main(["inbox"]) == 0
    assert "No new issue labelled `factory` in acme/ideas." in capsys.readouterr().out


def test_reject_and_show_a_blocked_item(cli_root, capsys):
    assert main(["intake", "Orders", "--idea", "an api", "--maturity", "poc"]) == 0
    assert main(["run", "orders"]) == 0
    assert main(["reject", "orders", "--as", "business", "--reason", "more detail"]) == 0
    store = Store(cli_root / "work")
    item = store.load("orders")
    item.status, item.feedback = "blocked", "gates failed:\n[tests] FAILED"
    store.save(item)
    capsys.readouterr()
    assert main(["show", "orders"]) == 0
    out = capsys.readouterr().out
    assert "feedback:" in out and "[tests] FAILED" in out and "report: work/orders/gate-report.md" in out


def test_promote_and_export(cli_root, tmp_path, capsys):
    ship()
    assert main(["promote", "orders", "--to", "mvp", "--as", "it"]) == 0
    assert "design_review" in capsys.readouterr().out
    assert main(["export", "orders", "--claudo", str(tmp_path / "claudo")]) == 0
    out = capsys.readouterr().out
    assert "Exported spec.md, design.md, tasks.md" in out
    assert (tmp_path / "claudo" / "work" / "orders" / "spec.md").is_file()
    assert main(["export", "orders", "--claudo", str(tmp_path / "other")]) == 0
    (cli_root / "work" / "orders" / "tasks.md").unlink()
    assert main(["export", "orders", "--claudo", str(tmp_path / "third")]) == 0
    assert "Exported spec.md, design.md to" in capsys.readouterr().out.splitlines()[-2]


def test_an_unsafe_host_run_builds_no_sandbox(cli_root, monkeypatch):
    built = []
    monkeypatch.setattr(cli, "Sandbox", lambda **kw: built.append(kw))
    assert main(["intake", "Orders", "--idea", "an api", "--maturity", "poc"]) == 0  # builds one too
    built.clear()
    assert main(["run", "orders", "--unsafe-host"]) == 0
    assert built == []
    assert main(["run", "orders"]) == 0
    assert built


def test_a_failing_demo_says_so(cli_root, monkeypatch, capsys):
    from factory import demo

    def broken(root, with_tests=False):
        raise demo.DemoError("radar guard missed the Flask dependency")

    monkeypatch.setattr(demo, "run_demo", broken)
    assert main(["demo"]) == 1
    assert "DEMO FAILED: radar guard missed" in capsys.readouterr().err


def test_output_never_fails_on_a_stream_that_cannot_be_reconfigured(monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    cli._utf8_console()  # StringIO has no reconfigure(): ignored
