"""Gate commands come from IT's golden_paths/ folder, never from the app the agent writes (A4-A7, A97)."""

import shutil

import pytest

from factory.foreman import FactoryError
from tests.test_drift import ship


def frontend_app_at_ship_review(foreman):
    item = foreman.run(foreman.intake("Visitor log", "A page listing visitors", "poc"))
    return foreman.approve(foreman.approve(item, "business"), "owner")


def test_an_agent_cannot_neuter_the_gates_by_editing_the_apps_golden_toml(foreman, executor):
    item = frontend_app_at_ship_review(foreman)
    app = foreman.app_dir(item)
    (app / "golden.toml").write_text('[gates.commands]\ntests = "true"\nlint = "true"\n', encoding="utf-8")
    executor.calls.clear()
    foreman._do_gate(item)
    ran = [c for c, _ in executor.calls]
    assert "true" not in ran
    assert any(c.endswith("npm --prefix web test") for c in ran)  # IT's command still runs


def test_a_broken_it_manifest_stops_the_gates_with_a_clear_error(foreman):
    item = frontend_app_at_ship_review(foreman)
    (foreman.cfg.golden_paths_dir / "fullstack-react" / "golden.toml").write_text("[gates", encoding="utf-8")
    with pytest.raises(FactoryError, match="not valid TOML.*IT must fix the golden path"):
        foreman._do_gate(item)


def test_gate_commands_that_are_not_a_table_are_refused(foreman):
    item = frontend_app_at_ship_review(foreman)
    manifest = foreman.cfg.golden_paths_dir / "fullstack-react" / "golden.toml"
    manifest.write_text('name = "fullstack-react"\n[gates]\ncommands = "true"\n', encoding="utf-8")
    with pytest.raises(FactoryError, match="must be a table"):
        foreman._do_gate(item)


def test_the_scaffold_comes_from_the_folder_even_when_the_manifest_names_it_otherwise(foreman):
    root = foreman.cfg.golden_paths_dir
    shutil.move(root / "fullstack-react", root / "react-v2")  # folder != manifest name
    item = frontend_app_at_ship_review(foreman)
    assert item.golden_path == "react-v2"
    assert (foreman.app_dir(item) / "web" / "src" / "App.tsx").is_file()
    assert foreman._gate_commands(item)["tests"].endswith("npm --prefix web test")


def test_an_item_scaffolded_before_the_path_was_recorded_still_gets_its_paths_commands(foreman):
    item = frontend_app_at_ship_review(foreman)
    item.golden_path = ""  # item.json written before the field existed
    assert foreman._gate_commands(item)["tests"].endswith("npm --prefix web test")


def test_an_app_without_a_golden_path_uses_the_factorys_commands(foreman):
    item = frontend_app_at_ship_review(foreman)
    item.golden_path = ""
    foreman._golden_path = lambda _item: None
    assert foreman._gate_commands(item) == {}


def test_a_change_is_gated_with_its_apps_golden_path_commands(foreman):
    app_item = ship(foreman, title="Visitor log", idea="A page listing visitors")
    change = foreman.intake_change(app_item.slug, "Add a filter", "filter the visitors by day", "feature")
    assert change.golden_path == ""  # a change scaffolds nothing: it inherits the app's path
    assert foreman._gate_commands(change)["tests"].endswith("npm --prefix web test")
