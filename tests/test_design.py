from factory.design import choose_stack, detect_capabilities, render_design


def test_capabilities_from_idea():
    caps = detect_capabilities("A dashboard where sales reps save their visit reports")
    assert {"backend", "frontend", "database"} <= set(caps)
    assert "messaging" not in caps


def test_stack_uses_preferred_adopted_tech(radar):
    choice = choose_stack(radar, ["backend", "database"], "prod", mentioned=[])
    assert choice.stack["backend"].id == "fastapi"
    assert choice.stack["database"].id == "postgresql"
    assert choice.needs_approval == [] and choice.gaps == []


def test_business_request_for_hold_tech_is_replaced(radar):
    mentioned = radar.scan_text("please use MongoDB")
    choice = choose_stack(radar, ["database"], "poc", mentioned)
    assert choice.stack["database"].id == "postgresql"
    assert [(a.id, b.id) for a, b in choice.replaced] == [("mongodb", "postgresql")]


def test_business_request_for_trial_tech_needs_approval_at_mvp(radar):
    mentioned = radar.scan_text("build it with Django")
    poc = choose_stack(radar, ["backend"], "poc", mentioned)
    assert poc.stack["backend"].id == "django" and poc.needs_approval == []
    mvp = choose_stack(radar, ["backend"], "mvp", mentioned)
    assert [t.id for t in mvp.needs_approval] == ["django"]


def test_rendered_design_lists_forbidden_in_ignore_block(radar):
    choice = choose_stack(radar, ["backend"], "prod", [])
    text = render_design(
        slug="x",
        title="X",
        maturity="prod",
        radar=radar,
        choice=choice,
        gates=["radar", "tests"],
        spec_version="0.1.0",
    )
    assert "| backend | FastAPI | adopt | tech radar |" in text
    start, end = text.index("<!-- radar:ignore -->"), text.index("<!-- /radar:ignore -->")
    assert "Flask" in text[start:end]
    assert "Flask" not in text[:start] + text[end:]
