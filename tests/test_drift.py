import shutil
from pathlib import Path

import pytest

from factory.cli import main
from factory.drift import radar_diff, render_diff, scan_drift
from factory.radar import load_radar

REPO = Path(__file__).resolve().parents[1]


def radar_with(tmp_path: Path, edits: dict[str, str] | None = None, extra: str = "", name: str = "r.toml"):
    """A copy of the real radar with ring edits ({tech id: new ring}) and extra TOML appended."""
    text = (REPO / "radar.toml").read_text(encoding="utf-8")
    for tech_id, ring in (edits or {}).items():
        head, _, tail = text.partition(f'id = "{tech_id}"')
        assert tail, tech_id
        ring_line = next(ln for ln in tail.splitlines() if ln.startswith("ring = "))
        text = head + f'id = "{tech_id}"' + tail.replace(ring_line, f'ring = "{ring}"', 1)
    path = tmp_path / name
    path.write_text(text + extra, encoding="utf-8")
    return load_radar(path), path


# ------------------------------------------------------------------ radar diff


def test_identical_radars_have_no_diff(radar, tmp_path):
    same, _ = radar_with(tmp_path)
    diff = radar_diff(radar, same)
    assert diff.empty and "No change" in render_diff(diff, radar, same)


def test_a_technology_moving_to_hold_is_reported_with_its_per_maturity_impact(radar, tmp_path):
    new, _ = radar_with(tmp_path, {"fastapi": "hold"})
    diff = radar_diff(radar, new)
    assert [(t.id, a, b) for t, a, b in diff.ring_changes] == [("fastapi", "adopt", "hold")]
    assert {(i.maturity, i.after) for i in diff.impacts if i.tech.id == "fastapi"} == {
        ("pov", "block"),
        ("poc", "block"),
        ("mvp", "block"),
        ("prod", "block"),
    }


def test_demotion_from_adopt_to_trial_is_stricter_only_from_mvp(radar, tmp_path):
    new, _ = radar_with(tmp_path, {"react": "trial"})
    impacts = [
        (i.maturity, i.before, i.after) for i in radar_diff(radar, new).impacts if i.tech.id == "react"
    ]
    assert impacts == [("mvp", "allow", "needs_it_approval"), ("prod", "allow", "needs_it_approval")]


def test_promotions_are_a_change_but_not_stricter(radar, tmp_path):
    new, _ = radar_with(tmp_path, {"flask": "adopt"})
    diff = radar_diff(radar, new)
    assert [(t.id, a, b) for t, a, b in diff.ring_changes] == [("flask", "hold", "adopt")]
    assert diff.impacts == []  # nothing became stricter


def test_added_and_removed_technologies(radar, tmp_path):
    extra = (
        '\n[[tech]]\nid = "rust"\nname = "Rust"\ncategory = "language"\nring = "trial"\nmatch = ["rust"]\n'
    )
    new, _ = radar_with(tmp_path, extra=extra)
    assert [t.id for t in radar_diff(radar, new).added] == ["rust"]
    gone = radar_diff(new, radar)  # the reverse direction removes it
    assert [t.id for t in gone.removed] == ["rust"]
    # trial: allowed at pov/poc; once off the radar it needs IT approval there, and prod blocks it
    assert {(i.maturity, i.after) for i in gone.impacts if i.tech.id == "rust"} == {
        ("pov", "needs_it_approval"),
        ("poc", "needs_it_approval"),
        ("prod", "block"),  # at mvp 'trial' already needed approval: unchanged, so not reported
    }
    text = render_diff(gone, new, radar)
    assert "- rust" in text and "not on radar" in text


# ------------------------------------------------------------------ drift scan


def ship(foreman, title="Orders", idea="an api", maturity="poc"):
    item = foreman.run(foreman.intake(title, idea, maturity))
    item = foreman.approve(item, "business")
    if item.stage == "design_review":
        item = foreman.approve(item, "it")
    item = foreman.approve(item, "owner")
    return foreman.approve(item, "it")


def test_a_compliant_shipped_app_is_not_drift(foreman):
    item = ship(foreman)
    drifted, clean = scan_drift(foreman.store.all(), foreman.radar, foreman.cfg.apps_dir)
    assert drifted == [] and [i.slug for i in clean] == [item.slug]


def test_when_it_moves_a_technology_to_hold_the_apps_using_it_drift(foreman, tmp_path):
    item = ship(foreman)
    new, _ = radar_with(tmp_path, {"fastapi": "hold"})
    drifted, clean = scan_drift(foreman.store.all(), new, foreman.cfg.apps_dir)
    assert clean == [] and [d.item.slug for d in drifted] == [item.slug]
    assert {v.key for v in drifted[0].violations} >= {"fastapi"}
    assert all(v.verdict == "block" for v in drifted[0].violations)


def test_only_shipped_apps_are_scanned(foreman, tmp_path):
    foreman.run(foreman.intake("Pending", "an api", "poc"))  # waiting for the business, nothing built
    new, _ = radar_with(tmp_path, {"fastapi": "hold"})
    assert scan_drift(foreman.store.all(), new, foreman.cfg.apps_dir) == ([], [])


def test_an_it_exception_keeps_an_app_compliant_until_it_is_removed(foreman, tmp_path):
    ship(foreman)
    new, _ = radar_with(tmp_path, {"react": "trial", "fastapi": "trial"})
    item = foreman.store.all()[0]
    item.maturity = "mvp"  # a trial technology needs IT approval from MVP up
    drifted, _ = scan_drift([item], new, foreman.cfg.apps_dir)
    keys = {v.key for d in drifted for v in d.violations}
    assert "fastapi" in keys and all(v.verdict == "needs_it_approval" for d in drifted for v in d.violations)
    item.it_exceptions = sorted(keys)  # IT explicitly approved what it saw
    assert scan_drift([item], new, foreman.cfg.apps_dir)[0] == []
    hold, _ = radar_with(tmp_path, {"fastapi": "hold"}, name="hold.toml")  # a hold is never excepted
    assert scan_drift([item], hold, foreman.cfg.apps_dir)[0] != []


def test_a_migration_is_opened_once_and_runs_on_the_target_app_not_a_new_one(foreman, tmp_path):
    item = ship(foreman)
    new, _ = radar_with(tmp_path, {"fastapi": "hold"})
    drifted, _ = scan_drift(foreman.store.all(), new, foreman.cfg.apps_dir)
    foreman.radar = new
    mig = foreman.open_migration(drifted[0])
    assert mig.kind == "migration" and mig.target == item.slug and mig.requester == "radar-drift"
    assert mig.status == "active" and mig.maturity == item.maturity and "FastAPI" in mig.idea
    assert foreman.open_migration(drifted[0]) is None  # idempotent: a change is already open for that app
    ran = foreman.run(foreman.store.load(mig.slug))
    assert (ran.stage, ran.status) == ("spec_review", "waiting")  # a real, governed pipeline
    assert not (foreman.cfg.apps_dir / mig.slug).exists()  # it works on the existing app's folder
    assert (foreman.store.dir(mig.slug) / "idea.md").is_file()


def test_drift_ignores_migration_items_themselves(foreman, tmp_path):
    ship(foreman)
    new, _ = radar_with(tmp_path, {"fastapi": "hold"})
    drifted, _ = scan_drift(foreman.store.all(), new, foreman.cfg.apps_dir)
    foreman.radar = new
    foreman.open_migration(drifted[0])
    drifted_again, clean = scan_drift(foreman.store.all(), new, foreman.cfg.apps_dir)
    assert len(drifted_again) == 1 and all(d.item.kind == "app" for d in drifted_again) and clean == []


# ------------------------------------------------------------------ CLI


@pytest.fixture
def cli_env(factory_root, monkeypatch):
    monkeypatch.setenv("AI_FACTORY_ROOT", str(factory_root))
    toml = factory_root / "factory.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace(', "tests"', "", 1), encoding="utf-8"
    )  # no uv here
    return factory_root


def test_cli_drift_is_clean_then_fails_after_a_radar_change_and_can_open_a_migration(cli_env, capsys):
    assert main(["intake", "Orders", "--idea", "an api", "--maturity", "poc"]) == 0
    for argv in (
        ["run", "orders"],
        ["approve", "orders", "--as", "business"],
        ["approve", "orders", "--as", "owner"],
    ):
        assert main(argv) == 0
    assert main(["approve", "orders", "--as", "it"]) == 0
    capsys.readouterr()
    assert main(["drift"]) == 0 and "1 compliant, 0 drifted" in capsys.readouterr().out

    radar = cli_env / "radar.toml"
    adopt = 'id = "fastapi"\nname = "FastAPI"\ncategory = "backend"\nring = "adopt"'
    assert adopt in radar.read_text(encoding="utf-8")
    radar.write_text(
        radar.read_text(encoding="utf-8").replace(adopt, adopt.replace('"adopt"', '"hold"')), encoding="utf-8"
    )
    assert main(["drift"]) == 1  # CI-friendly: non-zero when an app drifted
    out = capsys.readouterr().out
    assert "0 compliant, 1 drifted" in out and "orders" in out and "FastAPI" in out
    assert main(["drift", "--open"]) == 1 and "migration item: migrate-orders" in capsys.readouterr().out
    # With the migration open, the app is "in flight": not judged again, but the check must NOT go green,
    # because the violation is still there until the migration is merged.
    assert main(["drift"]) == 1
    out = capsys.readouterr().out
    assert (
        "1 app(s) with a change in flight" in out
        and "migration pending: migrate-orders-to-the-current-radar (for orders)" in out
    )
    assert main(["drift", "--open"]) == 1  # nothing new to open: no duplicate migration
    assert "migration item" not in capsys.readouterr().out
    assert main(["board"]) == 0 and "migrate-orders" in capsys.readouterr().out


def test_cli_radar_diff(cli_env, tmp_path, capsys):
    old = tmp_path / "old.toml"
    shutil.copy2(cli_env / "radar.toml", old)
    radar = cli_env / "radar.toml"
    hold = 'id = "flask"\nname = "Flask"\ncategory = "backend"\nring = "hold"'
    assert hold in radar.read_text(encoding="utf-8")
    radar.write_text(
        radar.read_text(encoding="utf-8").replace(hold, hold.replace('"hold"', '"adopt"')), encoding="utf-8"
    )
    assert main(["radar-diff", str(old)]) == 0
    out = capsys.readouterr().out
    assert "~ flask" in out and "hold -> adopt" in out
    assert main(["radar-diff", str(old), str(old)]) == 0 and "No change" in capsys.readouterr().out
