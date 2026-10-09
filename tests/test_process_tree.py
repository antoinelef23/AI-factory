"""Stopping a Claudo run stops everything it started (audit A12, A65)."""

import os
import signal
import subprocess
import threading
import time
from types import SimpleNamespace

from factory import claudo
from factory.claudo import ClaudoEngine
from factory.sandbox import Sandbox
from tests.test_claudo_bridge import fake_claudo

SPAWNING_ORCH = """
import os, subprocess, sys, time
beat = os.environ["FAKE_HEARTBEAT"]
child = "import time\\nwhile True:\\n    open(%r, 'a').write('x')\\n    time.sleep(0.1)\\n" % beat
subprocess.Popen([sys.executable, "-c", child])   # an agent the orchestrator started
time.sleep(120)
"""


def heartbeat_size(path):
    return path.stat().st_size if path.exists() else 0


def test_a_timed_out_run_takes_its_agents_down_with_it(tmp_path):
    home = fake_claudo(tmp_path / "c")
    (home / "lab" / "engine" / "orchestrate.py").write_text(SPAWNING_ORCH, encoding="utf-8")
    beat = tmp_path / "beat"
    done = {}

    def run():
        done["r"] = ClaudoEngine(home).run_build(
            "x", tmp_path / "p", env={"FAKE_HEARTBEAT": str(beat)}, timeout=3
        )

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(30)
    # Killing only the orchestrator left the agent alive, holding the output pipe: the run never returned.
    assert "r" in done, "the run hung: an orphaned agent kept the orchestrator's output open"
    result = done["r"]
    assert result.outcome == "timeout"
    assert heartbeat_size(beat) > 0, "control: the agent was running"
    time.sleep(0.5)
    before = heartbeat_size(beat)
    time.sleep(1.0)
    assert heartbeat_size(beat) == before  # nothing writes any more: the agent died with the run


class FakeProc:
    pid = 4242

    def __init__(self, waits):
        self.waits, self.killed = list(waits), False

    def wait(self, timeout=None):
        if self.waits.pop(0):
            raise subprocess.TimeoutExpired("orch", timeout)
        return 0

    def kill(self):
        self.killed = True


def test_on_windows_the_whole_tree_is_killed_with_taskkill(monkeypatch):
    calls = []
    monkeypatch.setattr(claudo.os, "name", "nt")
    monkeypatch.setattr(claudo.subprocess, "run", lambda argv, **kw: calls.append(argv))
    proc = FakeProc([True, False])  # still alive after taskkill: the last resort kills it
    claudo._stop(proc)
    assert calls == [["taskkill", "/T", "/F", "/PID", "4242"]] and proc.killed


def test_on_posix_the_group_gets_sigterm_then_sigkill(monkeypatch):
    sent = []
    monkeypatch.setattr(claudo.os, "name", "posix")
    monkeypatch.setattr(claudo.os, "killpg", lambda pid, sig: sent.append((pid, sig)), raising=False)
    monkeypatch.setattr(claudo.signal, "SIGKILL", 9, raising=False)
    claudo._stop(FakeProc([False]))  # SIGTERM was enough
    assert sent == [(4242, signal.SIGTERM)]
    sent.clear()
    claudo._stop(FakeProc([True, False]))  # ignored SIGTERM: the group is killed
    assert sent == [(4242, signal.SIGTERM), (4242, 9)]


def test_a_group_already_gone_is_not_an_error(monkeypatch):
    def gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(claudo.os, "killpg", gone, raising=False)
    claudo._signal_group(FakeProc([]), signal.SIGTERM)  # no exception


def test_reap_kills_every_container_of_the_run():
    calls = []

    def docker(args):
        calls.append(args)
        return (0, "abc\ndef\n") if args[0] == "ps" else (0, "")

    assert Sandbox(docker=docker).reap("factory-x") == ["abc", "def"]
    assert calls == [["ps", "-q", "--filter", "label=lab.run=factory-x"], ["kill", "abc", "def"]]


def test_reap_with_nothing_left_kills_nothing():
    calls = []
    assert Sandbox(docker=lambda a: calls.append(a) or (0, "")).reap("factory-x") == []
    assert Sandbox(docker=lambda a: (1, "daemon down")).reap("factory-x") == []
    assert [c[0] for c in calls] == ["ps"]


def test_a_sandboxed_claudo_run_is_labelled_and_reaped(foreman):
    from tests.test_claudo_build import ScriptedClaudo, at_ship_review

    reaped = []

    class Box(Sandbox):
        def problems(self, *, need_credential=True):
            return []

        def reap(self, label):
            reaped.append(label)
            return ["c1"]

        def executor(self, command, cwd):  # the gates "run in the container"
            return 0, "ok"

    foreman.sandbox = Box(docker=lambda a: (0, ""))
    engine = ScriptedClaudo()
    item = at_ship_review(foreman, engine)
    assert engine.runs[0]["env"]["LAB_SANDBOX_LABEL"] == f"factory-{item.slug}"
    assert reaped == [f"factory-{item.slug}"]
    assert any(h["event"] == "reaped" for h in item.history)


def test_the_process_group_flag_matches_the_os():
    expected = "creationflags" if os.name == "nt" else "start_new_session"
    assert list(claudo.OWN_PROCESS_GROUP) == [expected]
    assert SimpleNamespace(**claudo.OWN_PROCESS_GROUP)
