from pathlib import Path

import pytest

from factory.claudo import (
    ClaudoEngine,
    EngineError,
    LintResult,
    discover,
    is_claudo,
    parse_lint_output,
)

# A copy of what Claudo really prints (captured from `orchestrate.py --validate`).
REAL_OK = (
    "Plan-lint — 1 nodes, frontmatter status: approved\n"
    "  ⚠️  T1: no executable verify — done_when will not be checked mechanically\n"
    "  ⚠️  no checkpoint — a plan without a human pause violates CLAUDE.md (hard rules)\n"
)
REAL_BAD = (
    "Plan-lint — 0 nodes, frontmatter status: proposed\n"
    "  ❌ no task recognized — check the format `### T1 — title`\n"
)


def test_parse_warnings_only_is_ok():
    r = parse_lint_output(REAL_OK, 0)
    assert r.ok and r.errors == [] and len(r.warnings) == 2
    assert r.warnings[0].startswith("T1: no executable verify")


def test_parse_errors():
    r = parse_lint_output(REAL_BAD, 1)
    assert not r.ok and r.errors == ["no task recognized — check the format `### T1 — title`"]
    assert "- no task recognized" in r.feedback()


def test_nonzero_exit_without_parsable_error_is_still_an_error():
    r = parse_lint_output("Traceback: boom", 3)
    assert not r.ok and "exited 3" in r.errors[0] and "boom" in r.errors[0]


def test_feedback_lists_errors_then_warnings():
    r = LintResult(errors=["T1: done_when missing"], warnings=["T2: empty prompt"])
    assert r.feedback() == "- T1: done_when missing\n- (warning) T2: empty prompt"


def fake_claudo(tmp_path: Path) -> Path:
    (tmp_path / "lab" / "engine").mkdir(parents=True)
    (tmp_path / "lab" / "engine" / "orchestrate.py").write_text("# stub", encoding="utf-8")
    return tmp_path


def test_discover_prefers_explicit_config_and_rejects_a_bad_one(tmp_path):
    home = fake_claudo(tmp_path / "c")
    assert discover(str(home), tmp_path) == home.resolve()
    with pytest.raises(EngineError, match="no lab"):
        discover(str(tmp_path / "nope"), tmp_path)


def test_discover_env_then_sibling_then_none(tmp_path, monkeypatch):
    factory = tmp_path / "factory"
    factory.mkdir()
    monkeypatch.delenv("CLAUDO_HOME", raising=False)
    assert discover(None, factory) is None
    sibling = fake_claudo(tmp_path / "Claudo")
    assert discover(None, factory) == sibling.resolve()
    envhome = fake_claudo(tmp_path / "elsewhere")
    monkeypatch.setenv("CLAUDO_HOME", str(envhome))
    assert discover(None, factory) == envhome.resolve()  # env wins over the sibling guess


def test_engine_requires_a_claudo_checkout(tmp_path):
    assert not is_claudo(tmp_path)
    with pytest.raises(EngineError):
        ClaudoEngine(tmp_path)


FAKE_ORCH = """
import os, sys, time
mode = os.environ["FAKE_MODE"]
print("Plan: 2 nodes", flush=True)
print("▶ T1 (attempt 1/3)", flush=True)
if mode == "checkpoint":
    print("⏸  CP-1 — waiting for X/.approvals/CP-1 (or .rejected)", flush=True)
    time.sleep(120)            # a real orchestrator polls forever here
elif mode == "done":
    print("✅ run complete")
elif mode == "fail":
    print("❌ T1 failed"); sys.exit(3)
elif mode == "hang":
    time.sleep(120)
"""


def fake_orch_home(tmp_path: Path) -> Path:
    home = fake_claudo(tmp_path)
    (home / "lab" / "engine" / "orchestrate.py").write_text(FAKE_ORCH, encoding="utf-8")
    return home


@pytest.mark.parametrize(
    ("mode", "outcome", "cp"),
    [("checkpoint", "checkpoint", "CP-1"), ("done", "done", ""), ("fail", "failed", "")],
)
def test_run_build_outcomes(tmp_path, mode, outcome, cp):
    import time

    t0 = time.monotonic()
    r = ClaudoEngine(fake_orch_home(tmp_path / "c")).run_build(
        "x", tmp_path / "proj", env={"FAKE_MODE": mode}
    )
    assert (r.outcome, r.checkpoint) == (outcome, cp)
    assert "▶ T1" in r.log
    assert r.ok == (outcome != "failed")
    if mode == "checkpoint":
        assert time.monotonic() - t0 < 60  # the orchestrator was stopped, not waited for (it sleeps 120 s)


def test_run_build_can_wait_through_a_checkpoint_when_asked(tmp_path):
    """stop_at_checkpoint=False keeps Claudo's own blocking behavior (until the timeout here)."""
    r = ClaudoEngine(fake_orch_home(tmp_path / "c")).run_build(
        "x", tmp_path / "p", stop_at_checkpoint=False, env={"FAKE_MODE": "checkpoint"}, timeout=2
    )
    assert r.outcome == "timeout" and "CP-1" in r.log


def test_run_build_times_out_a_silent_orchestrator(tmp_path):
    r = ClaudoEngine(fake_orch_home(tmp_path / "c")).run_build(
        "x", tmp_path / "p", env={"FAKE_MODE": "hang"}, timeout=2
    )
    assert r.outcome == "timeout" and not r.ok


def test_lint_plan_runs_the_engine_on_a_throwaway_project(tmp_path, monkeypatch):
    import factory.claudo as claudo

    home = fake_claudo(tmp_path / "c")
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        project = Path(argv[argv.index("--project") + 1])
        seen["files"] = sorted(p.name for p in (project / "work" / "x").iterdir())
        seen["tasks"] = (project / "work" / "x" / "tasks.md").read_text(encoding="utf-8")
        return type("P", (), {"returncode": 0, "stdout": REAL_OK, "stderr": ""})()

    monkeypatch.setattr(claudo.subprocess, "run", fake_run)
    r = ClaudoEngine(home).lint_plan("x", spec="S", design="D", tasks="### T1 — é\n")
    assert r.ok and len(r.warnings) == 2
    assert seen["files"] == ["design.md", "spec.md", "tasks.md"] and seen["tasks"] == "### T1 — é\n"
    assert seen["argv"][-1] == "--validate" and seen["argv"][2] == "work/x"
    assert seen["kw"]["cwd"] == home and seen["kw"]["encoding"] == "utf-8"
    assert seen["kw"]["env"]["LAB_NO_NOTIFY"] == "1"
