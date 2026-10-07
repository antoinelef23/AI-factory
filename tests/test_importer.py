import json
import tomllib
from pathlib import Path

import pytest

from factory.cli import main
from factory.importer import import_radar
from factory.radar import load_radar, verdict

BYOR = """name,ring,quadrant,isNew,description
Python,Adopt,Languages & Frameworks,FALSE,Our default language
FastAPI,Adopt,Languages & Frameworks,TRUE,Standard for APIs
Django,Trial,Languages & Frameworks,FALSE,Existing apps only
SQLite,Assess,Platforms,FALSE,Prototypes
Flask,Hold,Languages & Frameworks,FALSE,Replaced
Terraform,Adopt,Tools,FALSE,IaC
"""

# What French Excel really writes: BOM, ';' delimiter, French ring names, accents.
FRENCH = (
    "﻿Nom;Anneau;Quadrant;Catégorie;Remplacement\n"
    "FastAPI;Adopter;Plateformes;backend;\n"
    "Flask;Suspendre;Plateformes;backend;FastAPI\n"
    "Django;Essayer;Plateformes;backend;\n"
    "Élasticsearch;Évaluer;Outils;search;\n"
)


def loaded(result, tmp_path: Path):
    path = tmp_path / "radar.toml"
    path.write_text(result.toml, encoding="utf-8")
    return load_radar(path)  # the factory's own loader must accept what we generate


def test_byor_csv_becomes_a_radar_the_factory_loads(tmp_path):
    res = import_radar(BYOR, company="Acme", version="2026.10")
    assert res.ok and res.counts == {"adopt": 3, "trial": 1, "assess": 1, "hold": 1}
    radar = loaded(res, tmp_path)
    assert (radar.company, radar.version) == ("Acme", "2026.10")
    assert {t.id: t.ring for t in radar.techs} == {
        "python": "adopt",
        "fastapi": "adopt",
        "django": "trial",
        "sqlite": "assess",
        "flask": "hold",
        "terraform": "adopt",
    }


def test_the_imported_radar_enforces_exactly_like_a_hand_written_one(tmp_path):
    radar = loaded(import_radar(BYOR), tmp_path)
    assert verdict(radar.get("flask"), "poc") == "block"
    assert verdict(radar.get("django"), "mvp") == "needs_it_approval"
    assert radar.find("FastAPI").id == "fastapi" and [t.id for t in radar.scan_text("built with Flask")] == [
        "flask"
    ]


def test_french_excel_export_with_bom_semicolons_and_french_rings(tmp_path):
    res = import_radar(FRENCH, source="radar-fr.csv")
    assert res.ok, res.problems
    radar = loaded(res, tmp_path)
    assert {t.id: t.ring for t in radar.techs} == {
        "fastapi": "adopt",
        "flask": "hold",
        "django": "trial",
        "elasticsearch": "assess",
    }
    assert radar.get("flask").replaced_by == "fastapi"  # 'Remplacement' column, resolved by name
    assert radar.get("elasticsearch").name == "Élasticsearch"  # accents survive
    assert radar.get("fastapi").category == "backend"  # explicit 'Catégorie' beats the quadrant hint
    assert res.needs_category == []


def test_a_tab_separated_export_is_detected(tmp_path):
    res = import_radar("name\tring\nRust\tTrial\nGo\tAdopt\n")
    assert res.ok and res.counts["trial"] == 1 and res.counts["adopt"] == 1


def test_json_export_with_a_blips_list(tmp_path):
    data = {"blips": [{"name": "Kafka", "ring": "Trial"}, {"name": "Flask", "ring": "Hold"}]}
    res = import_radar(json.dumps(data), source="radar.json")
    assert res.ok and [t.id for t in loaded(res, tmp_path).techs] == ["kafka", "flask"]


def test_quadrant_is_only_a_hint_and_missing_categories_are_reported_honestly():
    res = import_radar(BYOR)
    assert res.needs_category == ["python", "fastapi", "django", "sqlite", "flask", "terraform"]
    assert any("cannot be chosen for a stack" in w for w in res.warnings)
    assert 'category = "framework"' in res.toml and 'category = "platform"' in res.toml


def test_aliases_column_extends_the_match_list(tmp_path):
    res = import_radar("name,ring,match\nPostgreSQL,Adopt,postgres;psycopg\n")
    assert loaded(res, tmp_path).find("psycopg").id == "postgresql"
    assert "postgres" in tomllib.loads(res.toml)["tech"][0]["match"]


# ------------------------------------------------------------------ problems


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("", "no technology found"),
        ("foo,bar\n1,2\n", "needs a name column"),
        ("name,ring\nX,Maybe\n", "ring 'Maybe'"),
        ("name,ring\nX,Adopt\nx,Hold\n", "duplicates"),
        ("name,ring,replaced_by\nFlask,Hold,Ghost\n", "replaced_by 'ghost' is not on the radar"),
        ("name,ring\n,Adopt\n", "without a name"),
    ],
)
def test_bad_input_is_refused_with_a_line_number_or_reason(text, needle):
    res = import_radar(text)
    assert not res.ok and any(needle in p for p in res.problems), res.problems


def test_one_bad_row_blocks_the_whole_import_but_reports_every_problem():
    res = import_radar("name,ring\nA,Adopt\nB,Nope\nC,Nada\n")
    assert [p.split(":")[0] for p in res.problems] == ["line 3", "line 4"]


def test_blank_rows_are_ignored():
    assert import_radar("name,ring\nA,Adopt\n,\n\nB,Hold\n").counts["adopt"] == 1


def test_hostile_text_cannot_inject_toml(tmp_path):
    evil = 'name,ring\n"Evil""\nring = ""adopt""",Hold\n'
    res = import_radar(evil)
    if res.ok:  # whatever it parsed, the output must be valid TOML with a single, hold technology
        data = tomllib.loads(res.toml)
        assert [t["ring"] for t in data["tech"]] == ["hold"]


def test_special_characters_in_names_are_escaped(tmp_path):
    res = import_radar('name,ring\n"Say ""hi"" \\ ok",Adopt\n')
    assert res.ok and tomllib.loads(res.toml)["tech"][0]["name"] == 'Say "hi" \\ ok'


# ------------------------------------------------------------------ CLI


@pytest.fixture
def cli_env(factory_root, monkeypatch):
    monkeypatch.setenv("AI_FACTORY_ROOT", str(factory_root))
    return factory_root


def test_cli_imports_next_to_the_factory_without_touching_the_real_radar(cli_env, tmp_path, capsys):
    src = tmp_path / "company.csv"
    src.write_text(BYOR, encoding="utf-8")
    before = (cli_env / "radar.toml").read_text(encoding="utf-8")
    assert main(["radar-import", str(src), "--company", "Acme", "--version", "2026.10"]) == 0
    out = capsys.readouterr().out
    assert "Imported 6 technologies" in out and "adopt: 3" in out and "warning:" in out
    assert (cli_env / "radar.imported.toml").is_file()
    assert (cli_env / "radar.toml").read_text(encoding="utf-8") == before  # IT's radar untouched


def test_cli_refuses_to_overwrite_without_force(cli_env, tmp_path, capsys):
    src = tmp_path / "c.csv"
    src.write_text(BYOR, encoding="utf-8")
    target = tmp_path / "out.toml"
    assert main(["radar-import", str(src), "--out", str(target)]) == 0
    assert main(["radar-import", str(src), "--out", str(target)]) == 2
    assert "pass --force" in capsys.readouterr().err
    assert main(["radar-import", str(src), "--out", str(target), "--force"]) == 0


def test_cli_reports_problems_and_writes_nothing(cli_env, tmp_path, capsys):
    src = tmp_path / "bad.csv"
    src.write_text("name,ring\nX,Maybe\n", encoding="utf-8")
    assert main(["radar-import", str(src), "--out", str(tmp_path / "o.toml")]) == 2
    assert "line 2" in capsys.readouterr().err and not (tmp_path / "o.toml").exists()


def test_an_imported_radar_drives_a_whole_factory_item(cli_env, tmp_path):
    """The point of importing: swap it in and the factory enforces the company's own rules."""
    src = tmp_path / "company.csv"
    src.write_text(
        "name,ring,category,replaced_by\nPython,Adopt,language,\nFastAPI,Adopt,backend,\nFlask,Hold,backend,FastAPI\n"
        "pytest,Adopt,testing,\nPostgreSQL,Adopt,database,\n",
        encoding="utf-8",
    )
    assert main(["radar-import", str(src), "--out", str(cli_env / "radar.toml"), "--force"]) == 0
    assert main(["intake", "Notes", "--idea", "notes api built with Flask", "--maturity", "poc"]) == 0
    assert main(["run", "notes"]) == 0
    state = json.loads((cli_env / "work" / "notes" / "item.json").read_text(encoding="utf-8"))
    assert any("Flask" in n and "FastAPI" in n for n in state["notes"])  # the imported rules, applied
