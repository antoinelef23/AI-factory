"""The final Claudo run never polls for an hour (A13); the approval secret is private from birth (A14-A15)."""

import os

import pytest

from factory.claudo import BuildResult, load_or_create_secret, migrate_legacy_secret, secret_path
from tests.test_claudo_build import ScriptedClaudo, at_ship_review

CP1 = BuildResult("checkpoint", "CP-1", "T1 done")


def test_a_refused_token_blocks_the_ship_with_the_reason(foreman):
    engine = ScriptedClaudo([CP1, BuildResult("checkpoint", "CP-1", "token discarded, waiting at CP-1")])
    item = foreman.approve(at_ship_review(foreman, engine), "it", by="bob")
    assert (item.stage, item.status) == ("ship_review", "blocked")
    assert "did not accept the signed approval of CP-1" in item.feedback


def test_a_second_checkpoint_regates_the_new_work_then_asks_it_again(foreman, executor):
    engine = ScriptedClaudo(
        [CP1, BuildResult("checkpoint", "CP-2", "T2 done"), BuildResult("done", log="ok")]
    )
    item = at_ship_review(foreman, engine)
    first_nonce = item.approval_nonce
    executor.calls.clear()
    item = foreman.approve(item, "it", by="bob")
    assert (item.stage, item.status, item.claudo_cp) == ("ship_review", "waiting", "CP-2")
    assert executor.calls, "the work done after CP-1 went through the gates"
    assert item.approval_nonce != first_nonce and [a["role"] for a in item.approvals][-1] == "it"
    item = foreman.approve(item, "it", by="bob")
    assert item.status == "shipped" and [s["cp"] for s in engine.signed] == ["CP-1", "CP-2"]


def test_a_new_secret_is_private_and_a_legacy_one_is_moved_privately(tmp_path):
    path = secret_path(tmp_path)
    secret = load_or_create_secret(path)
    assert len(secret) == 64 and load_or_create_secret(path) == secret
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0 and path.parent.stat().st_mode & 0o077 == 0
    legacy = tmp_path / ".factory" / "approval-secret"
    legacy.parent.mkdir()
    legacy.write_text("legacy-secret\n", encoding="utf-8")
    path.unlink()
    assert migrate_legacy_secret(tmp_path) and path.read_text(encoding="utf-8") == "legacy-secret\n"
    assert not legacy.exists()
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0


def test_an_empty_secret_file_is_replaced(tmp_path):
    path = secret_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    assert len(load_or_create_secret(path)) == 64


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_FACTORY_STATE_DIR", str(tmp_path / "state"))
