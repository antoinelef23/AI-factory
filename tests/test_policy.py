"""Version constraints from uv.lock and licence policy from installed metadata (ROADMAP P3-3, P3-4)."""

from pathlib import Path

import pytest

from factory.gates import dependencies_gate
from factory.policy import PolicyError, check_dependencies, installed_licenses, locked_versions, satisfies
from factory.radar import RadarError, load_radar

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("version", "spec", "ok"),
    [
        ("0.115.4", ">=0.115", True),
        ("0.114.9", ">=0.115", False),
        ("1.0", ">=0.115, <1", False),
        ("0.200", ">=0.115, <1", True),
        ("2.7.1", "==2.7.*", True),
        ("2.8.0", "==2.7.*", False),
        ("2.8.0", "!=2.7.*", True),
        ("1.4.5", "~=1.4", True),
        ("2.0", "~=1.4", False),
        ("1.4.9", "~=1.4.2", True),
        ("1.5.0", "~=1.4.2", False),
        ("2.0rc1", ">=2.0", True),  # the release part is compared; pre-release tags are ignored
        ("3", "==3.0.0", True),
    ],
)
def test_satisfies(version, spec, ok):
    assert satisfies(version, spec) is ok


@pytest.mark.parametrize("spec", ["latest", ">= ", "=>1.0", "1.0"])
def test_a_malformed_constraint_is_an_error(spec):
    with pytest.raises(PolicyError):
        satisfies("1.0", spec)


# ------------------------------------------------------------------ an app on disk


def make_app(tmp_path, locked, licences=None):
    app = tmp_path / "app"
    app.mkdir()
    lines = ["version = 1", ""]
    for name, version in locked.items():
        lines += ["[[package]]", f'name = "{name}"', f'version = "{version}"', ""]
    (app / "uv.lock").write_text("\n".join(lines), encoding="utf-8")
    if licences is not None:
        site = app / ".venv" / "Lib" / "site-packages"
        site.mkdir(parents=True)
        for name, headers in licences.items():
            meta = site / f"{name}-1.0.dist-info"
            meta.mkdir()
            body = [f"Name: {name}", *headers, "", "Long description mentioning AGPL is not a licence."]
            (meta / "METADATA").write_text("\n".join(body), encoding="utf-8")
    return app


@pytest.fixture
def radar():
    return load_radar(REPO / "radar.toml")


def test_the_real_radar_pins_fastapi_and_forbids_copyleft_service_licences(radar):
    assert radar.get("fastapi").version == ">=0.115"
    assert "AGPL" in radar.forbidden_licenses and "SSPL" in radar.forbidden_licenses


def test_reading_a_lockfile(tmp_path):
    app = make_app(tmp_path, {"FastAPI": "0.115.0", "Pydantic_Core": "2.20"})
    assert locked_versions(app) == {"fastapi": "0.115.0", "pydantic-core": "2.20"}
    assert locked_versions(tmp_path) is None


def test_reading_licences_from_metadata_headers_only(tmp_path):
    app = make_app(
        tmp_path,
        {"a": "1"},
        {
            "a": ["License-Expression: MIT"],
            "b": ["License: UNKNOWN", "Classifier: License :: OSI Approved :: BSD License"],
            "c": [],
        },
    )
    assert installed_licenses(app) == {"a": "MIT", "b": "OSI Approved :: BSD License", "c": ""}
    assert installed_licenses(tmp_path) is None


def test_a_locked_version_outside_the_radar_constraint_is_a_violation(tmp_path, radar):
    app = make_app(tmp_path, {"fastapi": "0.100.0"}, {"fastapi": ["License-Expression: MIT"]})
    assert check_dependencies(app, radar) == ["fastapi 0.100.0 is locked; the radar allows FastAPI >=0.115"]


def test_a_constraint_pins_the_named_package_not_the_names_that_detect_it(tmp_path, radar):
    """Regression (real app, 2026-10-09): uvicorn 0.54 was compared with FastAPI's >=0.115."""
    app = make_app(tmp_path, {"fastapi": "0.143.0", "uvicorn": "0.54.0", "starlette": "0.50"}, {})
    assert check_dependencies(app, radar) == []


def test_a_forbidden_licence_is_found_in_a_classifier(tmp_path, radar):
    app = make_app(
        tmp_path,
        {"fastapi": "0.120", "evil": "1.0"},
        {
            "fastapi": ["License-Expression: MIT"],
            "evil": ["Classifier: License :: OSI Approved :: GNU Affero General Public License v3"],
        },
    )
    problems = check_dependencies(app, radar)
    assert len(problems) == 1 and problems[0].startswith("evil 1.0: licence") and "'Affero'" in problems[0]


def test_the_description_is_not_read_as_a_licence(tmp_path, radar):
    app = make_app(tmp_path, {"ok": "1"}, {"ok": ["License-Expression: MIT"]})  # body mentions AGPL
    assert check_dependencies(app, radar) == []


def test_a_lock_without_an_environment_cannot_be_checked_for_licences(tmp_path, radar):
    app = make_app(tmp_path, {"fastapi": "0.120"})
    assert check_dependencies(app, radar) == [
        "no installed environment (.venv) to read the licences from: run `uv sync` first"
    ]


def test_a_manifest_without_its_lockfile_fails_the_gate(tmp_path, radar):
    """Nothing pins what ships without a lockfile (audit A138): it used to read as "not applicable"."""
    assert dependencies_gate(tmp_path, radar).ok  # no manifest at all: nothing to pin
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text("{}", encoding="utf-8")
    result = dependencies_gate(tmp_path, radar)
    assert not result.ok and "pyproject.toml has no uv.lock" in result.detail
    assert "web/package.json has no package-lock.json" in result.detail
    (tmp_path / "locked").mkdir()
    app = make_app(tmp_path / "locked", {"fastapi": "0.100"}, {})
    assert not dependencies_gate(app, radar).ok


def test_the_radar_rejects_a_bad_constraint_or_licence_list(tmp_path):
    bad = tmp_path / "radar.toml"
    bad.write_text(
        '[licenses]\nforbidden = "AGPL"\n[[tech]]\nid = "x"\nname = "X"\ncategory = "backend"\n'
        'ring = "adopt"\nversion = "newest"\n',
        encoding="utf-8",
    )
    with pytest.raises(RadarError) as err:
        load_radar(bad)
    assert "not a version constraint" in str(err.value) and "forbidden must be a list" in str(err.value)


def test_the_gate_runs_from_mvp_up(factory_root):
    from factory.config import load_config

    cfg = load_config(factory_root)
    assert "dependencies" in cfg.gates_for("mvp") and "dependencies" in cfg.gates_for("prod")
    assert "dependencies" not in cfg.gates_for("poc")
