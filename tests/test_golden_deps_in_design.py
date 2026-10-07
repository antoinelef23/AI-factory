from pathlib import Path

from factory.detect import pyproject_dependencies
from factory.guard import check_project

GOLDEN = Path(__file__).resolve().parents[1] / "golden_paths" / "python-fastapi"


def design_of(foreman, maturity="poc"):
    item = foreman.run(foreman.intake("X", "an api", maturity))
    item = foreman.approve(item, "business")
    return foreman, item, foreman.store.read(item, "design.md")


def test_the_golden_path_dependencies_are_pre_approved_in_the_design(foreman):
    """Regression (live reject-path run): the golden path ships pydantic but design section 3 never said so,
    so an agent-written eval allowlist rejected it and the committed HEAD failed its own merge gate."""
    _, _, design = design_of(foreman)
    assert "Golden path dependencies (pre-approved by IT" in design
    for dep in ("fastapi", "uvicorn", "pydantic", "pytest", "httpx", "ruff"):
        assert f"| {dep} |" in design, dep
    assert "MUST allow these" in design


def test_the_section_lists_exactly_what_the_golden_pyproject_declares(foreman):
    _, _, design = design_of(foreman)
    declared = pyproject_dependencies((GOLDEN / "pyproject.toml").read_text(encoding="utf-8"))
    section = design.split("Golden path dependencies")[1].split("## 4.")[0]
    listed = [
        ln.split("|")[1].strip() for ln in section.splitlines() if ln.startswith("| ") and "---" not in ln
    ]
    assert sorted(listed[1:]) == sorted(declared)  # [0] is the table header


def test_known_techs_show_their_radar_ring_and_the_rest_say_golden_path(foreman):
    _, _, design = design_of(foreman)
    assert "| pydantic | Pydantic v2 (adopt) |" in design
    assert "| uvicorn | FastAPI (adopt) |" in design  # uvicorn is an alias of FastAPI on the radar


def test_a_golden_dependency_unknown_to_the_radar_is_labelled_as_pre_approved_by_the_golden_path(radar):
    from factory.design import choose_stack, render_design

    choice = choose_stack(radar, ["backend"], "poc", [])
    text = render_design(
        slug="x",
        title="X",
        maturity="poc",
        radar=radar,
        choice=choice,
        gates=["radar"],
        spec_version="0.1.0",
        golden_deps=["fastapi", "internal-sdk"],
    )
    assert "| fastapi | FastAPI (adopt) |" in text
    assert "| internal-sdk | pre-approved by the golden path |" in text


def test_adding_the_section_does_not_make_the_design_violate_the_radar(foreman):
    f, item, design = design_of(foreman)
    report = check_project(
        f.store.dir(item.slug), f.radar, "prod", docs=[f.store.dir(item.slug) / "design.md"]
    )
    assert report.ok(), [v.describe() for v in report.violations]


def test_a_design_without_a_golden_path_has_no_such_section(foreman, factory_root):
    import shutil

    shutil.rmtree(factory_root / "golden_paths" / "python-fastapi")
    _, _, design = design_of(foreman)
    assert "Golden path dependencies" not in design


def test_the_planner_is_told_to_allow_them(foreman):
    from tests.test_plan_lint_loop import GOOD, FakeEngine, Scripted, to_plan

    runner = Scripted([GOOD])
    to_plan(foreman, runner, FakeEngine())
    prompt = next(p for p in runner.prompts if "planner of" in p)
    assert "must\nallow the golden path dependencies listed in design.md section 3" in prompt
