"""The consumer process (worker.serve / worker.connect) and its place next to GET /health."""

import sys
import threading
import types

import pytest
from fastapi.testclient import TestClient

import worker
from worker import MemoryInbox, Message, connect, serve


def test_serve_drains_the_inbox_then_waits_until_stopped():
    inbox, stop = MemoryInbox([Message("a"), Message("b")]), threading.Event()
    waits = []

    def stop_after_one_idle_wait(seconds):
        waits.append(seconds)
        stop.set()
        return True

    stop.wait = stop_after_one_idle_wait
    assert serve(inbox, stop, idle_seconds=0.5) == 2 and waits == [0.5]


def test_without_an_adapter_there_is_no_inbox():
    assert connect() is None


def test_an_adapter_provides_the_inbox(monkeypatch):
    fake = types.ModuleType("worker.adapter")
    fake.connect = lambda: MemoryInbox([Message("orders")])
    monkeypatch.setitem(sys.modules, "worker.adapter", fake)
    assert isinstance(connect(), MemoryInbox)


def test_an_adapter_with_a_missing_dependency_is_an_error(monkeypatch):
    def broken(name):
        raise ModuleNotFoundError("No module named 'kafka'", name="kafka")

    monkeypatch.setattr(worker.importlib, "import_module", broken)
    with pytest.raises(ModuleNotFoundError):
        connect()


def test_the_app_runs_the_consumer_next_to_health(monkeypatch):
    inbox = MemoryInbox([Message("orders")])
    fake = types.ModuleType("worker.adapter")
    fake.connect = lambda: inbox
    monkeypatch.setitem(sys.modules, "worker.adapter", fake)
    from app.main import app

    with TestClient(app) as client:
        assert client.get("/health").json()["consumer"] == "running"
    assert inbox.acked == [Message("orders")]


def test_health_says_when_no_adapter_is_connected():
    from app.main import app

    with TestClient(app) as client:
        assert client.get("/health").json()["consumer"].startswith("no broker adapter")
