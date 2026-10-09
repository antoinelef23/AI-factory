"""Radar matching, exception keys and drift reporting (audit A82, A83, A88, A104, A105, A121-A123, A157)."""

from pathlib import Path

import pytest

from factory.cli import main
from factory.drift import radar_diff, render_diff, scan_drift
from factory.foreman import FactoryError
from factory.radar import RadarError, load_radar
from tests.test_drift import radar_with, ship

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def radar():
    return load_radar(REPO / "radar.toml")


def test_npm_angular_and_the_langchain_family_are_on_the_radar(radar):
    assert radar.find("angular").id == "angularjs"
    assert radar.find("langchain-community").id == "langchain"
    assert radar.find("langchain_openai").id == "langchain"
    assert radar.find("langchainish") is None  # a family needs its dash: no accidental prefix match


def test_motor_in_prose_is_not_mongodb_but_mongodb_still_is(radar):
    assert radar.scan_text("A quote tool for motor insurance") == []
    assert [t.id for t in radar.scan_text("store it in MongoDB")] == ["mongodb"]
    assert [t.id for t in radar.scan_text("uses `motor` as the async driver")] == ["mongodb"]
    assert [t.id for t in radar.scan_text("with the langchain-openai package")] == ["langchain"]


def write(tmp_path, body):
    path = tmp_path / "radar.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_an_alias_naming_two_technologies_is_refused(tmp_path):
    body = (
        '[[tech]]\nid = "a"\nname = "A"\ncategory = "x"\nring = "adopt"\nmatch = ["shared"]\n'
        '[[tech]]\nid = "b"\nname = "B"\ncategory = "x"\nring = "hold"\nmatch = ["Shared"]\n'
    )
    with pytest.raises(RadarError, match="alias 'shared' already names a"):
        load_radar(write(tmp_path, body))


def test_text_strict_may_only_name_the_techs_own_aliases(tmp_path):
    body = '[[tech]]\nid = "a"\nname = "A"\ncategory = "x"\nring = "adopt"\nmatch = ["a"]\n'
    body += 'text_strict = ["zz"]\n'
    with pytest.raises(RadarError, match="text_strict names"):
        load_radar(write(tmp_path, body))


def test_allow_stores_the_key_the_gate_reports_and_refuses_what_it_cannot_lift(foreman):
    item = foreman.intake("X", "an api", "mvp")
    foreman.allow(item, " Some_Lib ", "it", reason="needed")
    assert "some-lib" in item.it_exceptions
    with pytest.raises(FactoryError, match="LangChain is assess, so blocked at mvp: no exception lifts"):
        foreman.allow(item, "langchain-community", "it", reason="x")


def test_check_allow_entries_are_trimmed_and_normalised(factory_root, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AI_FACTORY_ROOT", str(factory_root))
    (tmp_path / "requirements.txt").write_text("Some_Lib\n", encoding="utf-8")
    assert main(["check", str(tmp_path), "--maturity", "mvp", "--allow", " some-lib , fastapi"]) == 0
    assert "OK (IT exception)" in capsys.readouterr().out


def test_radar_diff_reports_version_alias_and_licence_changes(tmp_path):
    old = load_radar(
        write(tmp_path, '[[tech]]\nid = "a"\nname = "A"\ncategory = "x"\nring = "adopt"\nmatch = ["a"]\n')
    )
    new_path = tmp_path / "new.toml"
    new_path.write_text(
        '[licenses]\nforbidden = ["GPL"]\n[[tech]]\nid = "a"\nname = "A"\ncategory = "x"\nring = "adopt"\n'
        'match = ["a", "a2"]\nversion = ">=2"\n',
        encoding="utf-8",
    )
    diff = radar_diff(old, load_radar(new_path))
    assert not diff.empty
    text = "\n".join(diff.other_changes)
    assert "a: version - -> >=2" in text and "a: aliases +['a2']" in text and "forbidden licences" in text
    assert "No change" not in render_diff(diff, old, load_radar(new_path))


def test_a_lost_alias_is_reported_too(tmp_path):
    old = load_radar(
        write(
            tmp_path, '[[tech]]\nid = "a"\nname = "A"\ncategory = "x"\nring = "adopt"\nmatch = ["a", "b"]\n'
        )
    )
    new_path = tmp_path / "new.toml"
    new_path.write_text(
        '[[tech]]\nid = "a"\nname = "A"\ncategory = "x"\nring = "adopt"\nmatch = ["a"]\n', encoding="utf-8"
    )
    assert "a: aliases -['b']" in radar_diff(old, load_radar(new_path)).other_changes


def test_a_shipped_app_whose_folder_is_gone_is_reported_as_drift(foreman, tmp_path):
    item = ship(foreman)
    foreman.radar, _ = radar_with(tmp_path, {})
    foreman.app_dir(item).rename(tmp_path / "moved-away")  # the folder was moved or renamed
    drifted, clean = scan_drift(foreman.store.all(), foreman.radar, foreman.cfg.apps_dir)
    assert clean == [] and drifted[0].violations[0].key == f"unverifiable:{item.slug}"
