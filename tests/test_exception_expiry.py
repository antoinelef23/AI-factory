from datetime import date, timedelta

import pytest

from factory.cli import main
from factory.drift import scan_drift
from factory.foreman import FactoryError
from factory.workitem import WorkItem

TODAY = date(2026, 10, 7)


def item_with(keys, terms=None):
    return WorkItem(
        slug="x", title="X", idea="i", maturity="mvp", it_exceptions=list(keys), exception_terms=terms or {}
    )


# ------------------------------------------------------------------ the model


def test_an_exception_without_an_expiry_is_always_active():
    assert item_with(["django"]).active_exceptions(TODAY + timedelta(days=9999)) == {"django"}


def test_the_expiry_day_itself_is_still_valid_and_the_next_day_is_not():
    it = item_with(["django"], {"django": {"expires": "2026-10-07"}})
    assert it.active_exceptions(TODAY) == {"django"}
    assert it.active_exceptions(TODAY + timedelta(days=1)) == set()
    assert it.expired_exceptions(TODAY + timedelta(days=1)) == ["django"]


def test_exceptions_expire_independently():
    it = item_with(
        ["django", "kafka"], {"django": {"expires": "2026-10-01"}, "kafka": {"expires": "2027-01-01"}}
    )
    assert it.active_exceptions(TODAY) == {"kafka"} and it.expired_exceptions(TODAY) == ["django"]


# ------------------------------------------------------------------ granting


def mvp_with_django(foreman):
    foreman.today = lambda: TODAY
    item = foreman.run(foreman.intake("Orders", "an orders api built with Django", "mvp"))
    item = foreman.approve(item, "business")
    assert item.stage == "design_review"
    return item


def test_a_design_review_exception_lapses_after_the_policy_default(foreman):
    item = foreman.approve(mvp_with_django(foreman), "it", by="bob", note="fine for this team")
    terms = item.exception_terms["django"]
    assert terms["expires"] == (TODAY + timedelta(days=180)).isoformat()
    assert terms["by"] == "bob" and "fine for this team" in terms["reason"]


def test_the_policy_can_make_design_exceptions_permanent(foreman):
    foreman.cfg.exception_days = 0
    item = foreman.approve(mvp_with_django(foreman), "it")
    assert item.exception_terms["django"]["expires"] == ""
    assert item.active_exceptions(TODAY + timedelta(days=5000)) == {"django"}


def test_allow_records_the_reason_the_approver_and_an_explicit_date(foreman):
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    foreman.today = lambda: TODAY
    item = foreman.allow(item, "kafka", "it", "bob", expires=date(2026, 12, 31), reason="event bus pilot")
    assert item.exception_terms["kafka"] == {
        "expires": "2026-12-31",
        "reason": "event bus pilot",
        "by": "bob",
    }
    assert "until 2026-12-31: event bus pilot" in item.history[-1]["detail"]


def test_allow_without_a_date_uses_the_policy_default(foreman):
    foreman.today = lambda: TODAY
    item = foreman.allow(foreman.run(foreman.intake("X", "an api", "poc")), "kafka", "it", reason="pilot")
    assert item.expiry_of("kafka") == (TODAY + timedelta(days=180)).isoformat()


def test_an_exception_cannot_expire_in_the_past(foreman):
    foreman.today = lambda: TODAY
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    with pytest.raises(FactoryError, match="in the past"):
        foreman.allow(item, "kafka", "it", expires=TODAY - timedelta(days=1), reason="x")
    assert item.it_exceptions == []


def test_renewing_an_exception_extends_it(foreman):
    foreman.today = lambda: TODAY
    item = foreman.allow(
        foreman.run(foreman.intake("X", "an api", "poc")), "kafka", "it", expires=TODAY, reason="a"
    )
    item = foreman.allow(
        item, "kafka", "it", expires=TODAY + timedelta(days=30), reason="renewed after review"
    )
    assert (
        item.it_exceptions == ["kafka"] and item.exception_terms["kafka"]["reason"] == "renewed after review"
    )
    assert item.active_exceptions(TODAY + timedelta(days=20)) == {"kafka"}


def test_hold_technologies_still_cannot_be_excepted(foreman):
    item = foreman.run(foreman.intake("X", "an api", "poc"))
    with pytest.raises(FactoryError, match="on hold"):
        foreman.allow(item, "mongodb", "it", reason="please")


# ------------------------------------------------------------------ enforcement


def test_the_build_gate_honors_an_active_exception(foreman):
    item = foreman.approve(mvp_with_django(foreman), "it")
    item = foreman.approve(item, "owner")
    assert (item.stage, item.status) == ("ship_review", "waiting")


def test_the_build_gate_rejects_a_technology_whose_exception_lapsed(foreman):
    item = foreman.approve(mvp_with_django(foreman), "it")
    foreman.today = lambda: (
        TODAY + timedelta(days=181)
    )  # the owner approves the plan after the exception expired
    item = foreman.approve(item, "owner")
    assert (item.stage, item.status) == ("build", "blocked")
    assert "Django" in item.feedback and "needs_it_approval" in item.feedback


def ship_mvp_with_django(foreman):
    item = foreman.approve(mvp_with_django(foreman), "it")
    item = foreman.approve(item, "owner")
    return foreman.approve(item, "it")


def test_drift_flags_an_app_once_its_exception_lapses_and_renewal_cures_it(foreman):
    item = ship_mvp_with_django(foreman)
    assert item.status == "shipped"
    apps = foreman.cfg.apps_dir
    # Drift judges what the app USES, not its design prose: the app really depends on Django.
    (apps / item.slug / "requirements.txt").write_text("django>=5\n", encoding="utf-8")
    assert scan_drift([item], foreman.radar, apps, today=TODAY + timedelta(days=100))[0] == []
    later = TODAY + timedelta(days=200)
    drifted, _ = scan_drift([item], foreman.radar, apps, today=later)
    assert [d.item.slug for d in drifted] == [item.slug]
    assert {v.key for v in drifted[0].violations} == {"django"}
    foreman.today = lambda: later
    item = foreman.allow(item, "django", "it", "bob", expires=later + timedelta(days=90), reason="renewed")
    assert scan_drift([item], foreman.radar, apps, today=later)[0] == []  # renewal cures the drift


# ------------------------------------------------------------------ CLI


@pytest.fixture
def cli_item(factory_root, monkeypatch):
    monkeypatch.setenv("AI_FACTORY_ROOT", str(factory_root))
    assert main(["intake", "Orders", "--idea", "an api", "--maturity", "poc"]) == 0
    return "orders"


def test_cli_allow_requires_a_reason(cli_item):
    with pytest.raises(SystemExit) as e:
        main(["allow", cli_item, "kafka", "--as", "it"])
    assert e.value.code == 2  # argparse: --reason is required


def test_cli_allow_shows_the_expiry_and_show_lists_it(cli_item, capsys):
    future = (date.today() + timedelta(days=30)).isoformat()
    assert (
        main(
            [
                "allow",
                cli_item,
                "kafka",
                "--as",
                "it",
                "--by",
                "bob",
                "--reason",
                "pilot",
                "--expires",
                future,
            ]
        )
        == 0
    )
    assert f"kafka: until {future}" in capsys.readouterr().out
    assert main(["show", cli_item]) == 0
    assert f"kafka (until {future})" in capsys.readouterr().out


def test_cli_rejects_a_malformed_or_past_expiry(cli_item, capsys):
    assert main(["allow", cli_item, "kafka", "--as", "it", "--reason", "x", "--expires", "31/12/2026"]) == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err
    assert main(["allow", cli_item, "kafka", "--as", "it", "--reason", "x", "--expires", "2001-01-01"]) == 2
    assert "in the past" in capsys.readouterr().err


def test_show_flags_expired_exceptions(cli_item, factory_root, capsys):
    from factory.config import load_config
    from factory.workitem import Store

    store = Store(load_config(factory_root).work_dir)
    it = store.load(cli_item)
    it.it_exceptions, it.exception_terms = (
        ["kafka"],
        {"kafka": {"expires": "2020-01-01", "reason": "old", "by": "bob"}},
    )
    store.save(it)
    assert main(["show", cli_item]) == 0
    out = capsys.readouterr().out
    assert "kafka (until 2020-01-01)" in out and "EXPIRED exceptions: kafka" in out
