"""The judge never crashes on a malformed answer and grounds only real evidence (A61, A145-A148, A64, A66)."""

import json
import subprocess

from factory.judge import absence_holds, combine, evaluate
from factory.project import modified_tests, prepare_project
from factory.templates import plan_prompt
from factory.workitem import WorkItem
from tests.test_judge_panel import ARTIFACT, report

SPEC = "# Spec\n\n## 1. Intent\n\nTrack orders.\n\n## 7. Evals\n\n"


def test_a_null_criteria_list_is_a_problem_not_a_crash():
    rep = evaluate("spec", json.dumps({"criteria": None, "summary": "s"}), ARTIFACT)
    assert rep.verdict == "unreliable" and "judge answer has no criteria list" in rep.problems


def test_a_criterion_id_that_is_not_a_string_is_ignored():
    rep = evaluate(
        "spec", json.dumps({"criteria": [{"id": ["fidelity"], "score": 3, "quote": "x"}]}), ARTIFACT
    )
    assert any("unexpected or duplicate criterion" in p for p in rep.problems)


def test_absent_must_name_a_part_of_the_artifact():
    haystack = "track orders"
    assert not absence_holds("ABSENT: Purple Unicorn Compliance", 1, haystack, SPEC)
    assert absence_holds("ABSENT: Examples", 1, haystack, SPEC)  # truly not there
    assert not absence_holds("ABSENT: Examples", 4, haystack, SPEC)  # an absence grounds a LOW score only
    assert not absence_holds("not an absence claim", 1, haystack, SPEC)


def test_a_heading_left_with_nothing_under_it_is_an_absence():
    haystack = "## 1. intent track orders. ## 7. evals"
    assert absence_holds("ABSENT: Evals table", 2, haystack, SPEC)
    full = SPEC + "| EVAL-1 | test | x | BHV-1 | 1 of 1 |\n"
    assert not absence_holds("ABSENT: Evals", 2, haystack, full)


def test_an_even_panel_takes_the_true_median_not_the_minimum():
    panel = combine("spec", [report([3, 3, 3, 3, 3, 3]), report([5, 5, 5, 5, 5, 5])])
    assert {c.score for c in panel.criteria} == {4}


def test_an_unreliable_panel_keeps_a_raw_answer_for_diagnosis():
    one, two = report([4, 4]), report([4, 4])  # too few criteria scored in each run
    one.raw = '{"criteria": []}'
    panel = combine("spec", [one, two, report([4, 4])])
    assert panel.verdict == "unreliable" and panel.raw


def test_a_rewritten_frontend_test_counts_as_a_modified_test(tmp_path):
    web = tmp_path / "web" / "src"
    web.mkdir(parents=True)
    (web / "health.test.ts").write_text("test('ok', () => {})\n", encoding="utf-8")
    (web / "health.ts").write_text("export const x = 1\n", encoding="utf-8")
    (tmp_path / "test_top.py").write_text("def test(): pass\n", encoding="utf-8")
    prepare_project(tmp_path)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.strip()
    (web / "health.test.ts").write_text("// weakened\n", encoding="utf-8")
    (web / "health.ts").write_text("export const x = 2\n", encoding="utf-8")
    (tmp_path / "test_top.py").unlink()
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "change"], cwd=tmp_path, check=True)
    assert sorted(modified_tests(tmp_path, base)) == [
        "test_top.py (deleted)",
        "web/src/health.test.ts (modified)",
    ]


def test_a_change_plan_uses_the_stack_section_of_a_change_design():
    item = WorkItem(slug="add", title="Add", idea="i", maturity="poc", kind="feature", target="orders")
    assert "design.md section 2 (the existing stack, kept)" in plan_prompt(item, "", change_of="orders")
    assert "design.md section 3" in plan_prompt(item, "")
