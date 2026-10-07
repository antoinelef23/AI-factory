from pathlib import Path

import pytest

from factory.radar import ALLOW, APPROVAL, BLOCK, RadarError, load_radar, normalize, verdict


def test_real_radar_loads(radar):
    assert radar.company
    assert radar.get("fastapi").ring == "adopt"
    assert radar.get("mongodb").replaced_by == "postgresql"


@pytest.mark.parametrize(
    ("tech_id", "maturity", "expected"),
    [
        ("fastapi", "prod", ALLOW),
        ("django", "poc", ALLOW),
        ("django", "mvp", APPROVAL),
        ("sqlite", "pov", ALLOW),
        ("sqlite", "poc", APPROVAL),
        ("sqlite", "prod", BLOCK),
        ("mongodb", "pov", BLOCK),
    ],
)
def test_policy_gets_stricter_with_maturity(radar, tech_id, maturity, expected):
    assert verdict(radar.get(tech_id), maturity) == expected


def test_unknown_tech_needs_approval_then_blocks_in_prod():
    assert verdict(None, "poc") == APPROVAL
    assert verdict(None, "prod") == BLOCK


def test_find_normalizes_package_names(radar):
    assert normalize("Psycopg_Binary") == "psycopg-binary"
    assert radar.find("Psycopg_Binary").id == "postgresql"
    assert radar.find("pymongo").id == "mongodb"
    assert radar.find("left-pad") is None


def test_scan_text_uses_word_boundaries(radar):
    found = {t.id for t in radar.scan_text("A FastAPI service on MongoDB, with a reactive pythonic UI")}
    assert {"fastapi", "mongodb"} <= found
    assert not {"react", "python"} & found  # 'reactive' is not 'react', 'pythonic' is not 'python'


def test_invalid_radar_is_rejected(tmp_path: Path):
    bad = tmp_path / "radar.toml"
    bad.write_text(
        '[[tech]]\nid = "x"\nname = "X"\ncategory = "backend"\nring = "maybe"\n'
        '[[tech]]\nid = "y"\nname = "Y"\ncategory = "backend"\nring = "hold"\nreplaced_by = "nope"\n',
        encoding="utf-8",
    )
    with pytest.raises(RadarError) as e:
        load_radar(bad)
    assert "ring 'maybe'" in str(e.value)
    assert "replaced_by 'nope'" in str(e.value)
