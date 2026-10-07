from pathlib import Path

import pytest

from factory.claudo import BuildResult, ClaudoEngine
from factory.foreman import FactoryError
from tests.test_claudo_build import PlanThenBuildAgent, ScriptedClaudo

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def place_report(project: Path, slug: str, text: str, cp: str = "CP-1") -> None:
    runs = project / "work" / slug / ".runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{cp}-review.md").write_text(text, encoding="utf-8")


# ------------------------------------------------------------------ reading the verdict


def test_the_real_block_report_from_a_live_run_is_read_correctly(tmp_path):
    """Fixture: the reviewer report Claudo (Opus) wrote in the live reject-path run."""
    place_report(tmp_path, "x", (FIXTURES / "CP-1-review-block.md").read_text(encoding="utf-8"))
    assert ClaudoEngine.review_verdict(tmp_path, "x", "CP-1") == ("BLOCK", "work/x/.runs/CP-1-review.md")


@pytest.mark.parametrize("verdict", ["PASS", "WARN", "BLOCK"])
def test_each_verdict_is_parsed(tmp_path, verdict):
    place_report(
        tmp_path, "x", f"# Review CP-1 - panel of 1 - aggregated verdict: {verdict} ({verdict})\n\nbody"
    )
    assert ClaudoEngine.review_verdict(tmp_path, "x", "CP-1")[0] == verdict


def test_no_report_means_none_and_an_unreadable_header_means_unknown(tmp_path):
    assert ClaudoEngine.review_verdict(tmp_path, "x", "CP-1") is None
    place_report(tmp_path, "x", "# something else entirely\n")
    assert ClaudoEngine.review_verdict(tmp_path, "x", "CP-1")[0] == "UNKNOWN"


def test_the_verdict_is_read_from_the_head_so_quoted_text_cannot_spoof_it(tmp_path):
    body = "# Review CP-1 - aggregated verdict: BLOCK (BLOCK)\n" + "x" * 2000 + "\naggregated verdict: PASS\n"
    place_report(tmp_path, "x", body)
    assert ClaudoEngine.review_verdict(tmp_path, "x", "CP-1")[0] == "BLOCK"


# ------------------------------------------------------------------ the human is informed, and must justify


def at_ship_review(foreman, verdict):
    engine = ScriptedClaudo([BuildResult("checkpoint", "CP-1", "x"), BuildResult("done", log="ok")])
    engine.review = (verdict, "work/x/.runs/CP-1-review.md")
    foreman.runner, foreman.engine = PlanThenBuildAgent(), engine
    foreman.cfg.claudo_build_from = "poc"
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    item = foreman.approve(foreman.approve(item, "business"), "owner")
    assert (item.stage, item.status) == ("ship_review", "waiting")
    return item


def test_the_verdict_is_recorded_on_the_item_for_the_human(foreman):
    item = at_ship_review(foreman, "BLOCK")
    assert item.claudo_review == {"cp": "CP-1", "verdict": "BLOCK", "report": "work/x/.runs/CP-1-review.md"}


@pytest.mark.parametrize("verdict", ["BLOCK", "WARN", "UNKNOWN"])
def test_shipping_over_a_non_pass_verdict_needs_a_justification(foreman, verdict):
    item = at_ship_review(foreman, verdict)
    with pytest.raises(FactoryError, match=f"reviewer said {verdict}.*--note"):
        foreman.approve(item, "it", by="bob")
    assert item.status == "waiting" and item.claudo_cp == "CP-1"  # nothing was signed or finalized


def test_a_whitespace_note_is_not_a_justification(foreman):
    item = at_ship_review(foreman, "BLOCK")
    with pytest.raises(FactoryError):
        foreman.approve(item, "it", by="bob", note="   ")


def test_with_a_justification_the_ship_goes_through_and_the_reason_is_recorded(foreman):
    item = at_ship_review(foreman, "BLOCK")
    item = foreman.approve(item, "it", by="bob", note="reviewer's point is cosmetic, tracked in the backlog")
    assert item.status == "shipped"
    assert item.approvals[-1]["note"] == "reviewer's point is cosmetic, tracked in the backlog"


def test_a_pass_verdict_ships_without_a_note(foreman):
    item = foreman.approve(at_ship_review(foreman, "PASS"), "it", by="bob")
    assert item.status == "shipped"


def test_rejecting_is_never_blocked_by_the_verdict_rule(foreman):
    item = at_ship_review(foreman, "BLOCK")
    item = foreman.reject(item, "it", "fix what the reviewer found", by="bob")
    assert item.stage == "build"


def test_show_prints_the_reviewer_verdict(foreman, capsys):
    from factory.cli import _print_item

    item = at_ship_review(foreman, "BLOCK")
    _print_item(foreman, item)
    assert "Claudo reviewer CP-1: BLOCK" in capsys.readouterr().out
