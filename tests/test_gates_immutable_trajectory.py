from pathlib import Path

import pytest

from factory.claudo import ClaudoEngine, discover
from tests.test_claudo_build import PlanThenBuildAgent, ScriptedClaudo

REPO = Path(__file__).resolve().parents[1]


def run_to_build(foreman, tamper=None, maturity="poc", engine=None):
    """Drive an item to the gate stage; `tamper(app, store_dir)` runs right after the build scaffolds."""
    foreman.runner = PlanThenBuildAgent() if engine else None
    foreman.engine = engine
    foreman.cfg.claudo_build_from = "poc"
    item = foreman.run(foreman.intake("X", "an api", maturity))
    item = foreman.approve(item, "business")
    if item.stage == "design_review":
        item = foreman.approve(item, "it")
    if tamper:
        real = foreman._do_build

        def build_then_tamper(it):
            out = real(it)
            tamper(foreman.app_dir(it) / "work" / it.slug, foreman.store.dir(it.slug))
            return out

        foreman._do_build = build_then_tamper
    return foreman.approve(item, "owner")


def test_untouched_spec_and_design_pass(foreman):
    item = run_to_build(foreman)
    assert (item.stage, item.status) == ("ship_review", "waiting")
    assert set(item.approved_hashes) == {"spec.md", "design.md"}
    assert "spec.md and design.md are as approved" in foreman.store.read(item, "gate-report.md")


def test_an_agent_weakening_the_spec_inside_the_app_is_caught(foreman):
    def weaken(app_triplet, _store):
        spec = app_triplet / "spec.md"
        spec.write_text(spec.read_text(encoding="utf-8").replace("MUST", "MAY"), encoding="utf-8")

    item = run_to_build(foreman, tamper=weaken)
    assert (item.stage, item.status) == ("build", "blocked")
    assert "spec.md: modified inside the app during the build" in item.feedback
    assert "FAIL" in foreman.store.read(item, "gate-report.md")


def test_an_agent_editing_the_design_inside_the_app_is_caught(foreman):
    def swap_stack(app_triplet, _store):
        design = app_triplet / "design.md"
        design.write_text(design.read_text(encoding="utf-8") + "\nUse Flask.\n", encoding="utf-8")

    item = run_to_build(foreman, tamper=swap_stack)
    assert item.status == "blocked" and "design.md: modified inside the app" in item.feedback


def test_a_spec_edited_in_the_store_after_approval_needs_a_new_approval(foreman):
    def amend(_app, store_dir):
        spec = store_dir / "spec.md"
        spec.write_text(spec.read_text(encoding="utf-8") + "\n- **INV-9** — new rule.\n", encoding="utf-8")

    item = run_to_build(foreman, tamper=amend)
    assert item.status == "blocked"
    assert "changed in work/x/ after its approval" in item.feedback and "approve again" in item.feedback


def test_a_deleted_copy_is_caught(foreman):
    item = run_to_build(foreman, tamper=lambda app_triplet, _s: (app_triplet / "spec.md").unlink())
    assert item.status == "blocked" and "spec.md: missing from the app" in item.feedback


def test_re_approving_an_amended_spec_updates_the_frozen_hash(foreman):
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    first = foreman.store.read(item, "spec.md")
    item = foreman.reject(item, "business", "add a rule")
    foreman.store.write(item, "spec.md", first + "\n- **INV-9** — amended.\n")
    item.stage = "spec_review"  # a regenerated spec waiting for approval again
    item = foreman.approve(item, "business")
    assert item.approved_hashes["spec.md"] == foreman._sha(foreman.store.read(item, "spec.md"))


def test_design_hash_is_recorded_when_review_is_skipped_and_when_it_is_approved(foreman):
    poc = run_to_build(foreman, maturity="poc")  # review skipped: compiled design is the approved one
    assert "design.md" in poc.approved_hashes
    foreman2_item = foreman.run(foreman.intake("Y", "an api", "mvp"))
    foreman2_item = foreman.approve(foreman2_item, "business")
    assert foreman2_item.stage == "design_review" and "design.md" not in foreman2_item.approved_hashes
    foreman2_item = foreman.approve(foreman2_item, "it")  # IT approval freezes the design
    assert foreman2_item.approved_hashes["design.md"] == foreman._sha(
        foreman.store.read(foreman2_item, "design.md")
    )


def test_legacy_items_without_hashes_are_not_failed(foreman):
    item = run_to_build(foreman)
    item.approved_hashes = {}
    assert foreman._immutability_gate(item).ok


# ------------------------------------------------------------------ trajectory


def test_trajectory_is_not_applicable_without_a_claudo_build(foreman):
    item = run_to_build(foreman, maturity="mvp")
    assert item.status == "waiting"
    assert "not applicable: this item was not built through Claudo" in foreman.store.read(
        item, "gate-report.md"
    )


def test_a_failing_trajectory_blocks_the_ship(foreman):
    engine = ScriptedClaudo()
    engine.trajectory_result = (False, "BHV-2a: T99 is `task_done` with no prior successful `task_attempt`")
    item = run_to_build(foreman, maturity="mvp", engine=engine)
    assert item.stage == "build" and item.status == "blocked" and "BHV-2a" in item.feedback


def test_a_clean_trajectory_passes(foreman):
    item = run_to_build(foreman, maturity="mvp", engine=ScriptedClaudo())
    assert item.status == "waiting" and "[trajectory] OK" in foreman.store.read(item, "gate-report.md")


def test_poc_does_not_run_the_trajectory_gate(factory_root):
    from factory.config import load_config

    cfg = load_config(factory_root)
    assert "trajectory" not in cfg.gates_for("poc") and "trajectory" in cfg.gates_for("mvp")
    assert all("immutable" in cfg.gates_for(m) for m in ("pov", "poc", "mvp", "prod"))


# ------------------------------------------------------------------ the real guard


HOME = discover(None, REPO)


@pytest.mark.skipif(HOME is None, reason="no Claudo checkout found (set CLAUDO_HOME)")
def test_the_real_trajectory_guard_catches_a_forged_journal(tmp_path):
    engine = ClaudoEngine(HOME)
    runs = tmp_path / "work" / "x" / ".runs"
    runs.mkdir(parents=True)
    (tmp_path / "work" / "x" / "tasks.md").write_text(
        "### T1 — a\n- **files_touched :** `a`\n", encoding="utf-8"
    )
    journal = runs / "journal.jsonl"
    journal.write_text(
        '{"event": "task_attempt", "id": "T1", "ok": true}\n{"event": "task_done", "id": "T1"}\n'
    )
    assert engine.trajectory(tmp_path, "x")[0] is True  # a healthy run
    journal.write_text(journal.read_text() + '{"event": "task_done", "id": "T99"}\n')
    ok, out = engine.trajectory(tmp_path, "x")
    assert ok is False and "T99" in out and "forged or corrupt" in out
