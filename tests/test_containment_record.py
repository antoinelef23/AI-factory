"""Agent code never moves silently from the sandbox to the host (H2: audit A46-A48)."""

import pytest

from factory.claudo import BuildResult
from factory.foreman import FactoryError
from factory.sandbox import Sandbox
from tests.test_claudo_build import ScriptedClaudo, at_ship_review


class ReadyBox(Sandbox):
    def problems(self, *, need_credential=True):
        return []

    def executor(self, command, cwd):
        return 0, "ok"

    def reap(self, label):
        return []


class DownBox(ReadyBox):
    def problems(self, *, need_credential=True):
        return ["Docker is not running (down): start Docker Desktop"]


def sandboxed_at_ship_review(foreman):
    foreman.sandbox = ReadyBox()
    engine = ScriptedClaudo(
        [BuildResult("checkpoint", "CP-1", "T1 done\n⏸  CP-1 — waiting for x"), BuildResult("done", log="ok")]
    )
    item = at_ship_review(foreman, engine)
    assert item.build_where == "sandbox" and item.claudo_cp
    return item


def test_approving_with_unsafe_host_records_the_move_and_needs_its_acknowledgement(foreman):
    item = sandboxed_at_ship_review(foreman)
    foreman.sandbox, foreman.unsafe_host = None, True  # `approve --unsafe-host`
    with pytest.raises(FactoryError, match="unsafe_host: Claudo's final run ran on the host"):
        foreman.approve(item, "it", by="bob")
    assert item.build_where == "host"
    assert "host, unsandboxed" in foreman._pr_body(item)


def test_the_acknowledged_move_ships(foreman):
    item = sandboxed_at_ship_review(foreman)
    foreman.sandbox, foreman.unsafe_host = None, True
    with pytest.raises(FactoryError):
        foreman.approve(item, "it", by="bob")
    item = foreman.approve(item, "it", by="bob", note="Docker broke on the build host; accepted for this one")
    assert item.status == "shipped" and item.build_where == "host"


def test_without_unsafe_host_a_down_sandbox_refuses_the_final_run(foreman):
    item = sandboxed_at_ship_review(foreman)
    foreman.sandbox = DownBox()
    with pytest.raises(
        FactoryError, match="(?s)Claudo's final run must run in the sandbox.*Docker is not running"
    ):
        foreman.approve(item, "it", by="bob")
    assert item.build_where == "sandbox" and not any(a["kind"] == "unsafe_host" for a in item.ship_acks)


def test_gates_after_a_sandboxed_build_wait_for_the_sandbox(foreman, executor):
    item = sandboxed_at_ship_review(foreman)
    item.stage, item.status = "gate", "active"
    foreman.sandbox = DownBox()
    executor.calls.clear()
    item = foreman.run(item)
    assert (item.stage, item.status) == ("gate", "blocked")
    assert "the gates (they run the agent's code) must run in the sandbox" in item.feedback
    assert executor.calls == []


def test_a_ready_sandbox_changes_nothing(foreman):
    item = sandboxed_at_ship_review(foreman)
    assert foreman._still_contained(item, "x", need_credential=True) == ""
    assert item.build_where == "sandbox" and not item.ship_acks


def test_an_item_built_on_the_host_is_not_rechecked(foreman):
    item = foreman.intake("X", "an api", "poc")
    item.build_where = "host"
    foreman.sandbox = DownBox()
    assert foreman._still_contained(item, "x", need_credential=False) == ""
