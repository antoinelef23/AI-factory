"""Inputs the factory cannot read are errors, not silent passes (H5: A17, A18, A59, A60, A87, slugify)."""

import json

import pytest

from factory.cli import main
from factory.design import detect_capabilities
from factory.importer import import_radar, tech_slug
from factory.radar import load_radar

OPTIONAL = {"frontend", "database", "ai", "messaging"}


def optional(idea):
    return set(detect_capabilities(idea)) & OPTIONAL


# ------------------------------------------------------------------ factory check


@pytest.fixture
def cli_root(factory_root, monkeypatch):
    monkeypatch.setenv("AI_FACTORY_ROOT", str(factory_root))
    return factory_root


def test_check_on_a_missing_path_fails(cli_root, capsys):
    assert main(["check", str(cli_root / "no-such-app")]) == 2
    assert "is not a directory: nothing was checked" in capsys.readouterr().err


def test_check_with_a_missing_doc_fails(cli_root, tmp_path, capsys):
    assert main(["check", str(tmp_path), "--doc", "design.md"]) == 2
    assert "--doc not found" in capsys.readouterr().err


# ------------------------------------------------------------------ radar import


def test_json_nulls_and_lists_are_not_written_as_python_reprs(tmp_path):
    data = [
        {"name": "FastAPI", "ring": "adopt", "category": None, "match": ["fast-api", "fastapi"]},
        {"name": "Flask", "ring": "hold", "replaced_by": None},
    ]
    result = import_radar(json.dumps(data), source="radar.json")
    assert result.ok, result.problems
    assert "None" not in result.toml and "['" not in result.toml
    out = tmp_path / "radar.toml"
    out.write_text(result.toml, encoding="utf-8")
    radar = load_radar(out)
    assert radar.get("fastapi").match[-1] == "fast-api" and radar.get("flask").replaced_by is None


def test_c_cpp_and_csharp_get_distinct_ids():
    assert [tech_slug(n) for n in ("C", "C++", "C#", "Notepad+")] == ["c", "cpp", "csharp", "notepadplus"]
    result = import_radar("name,ring\nC,adopt\nC++,adopt\nC#,trial\n")
    assert result.ok, result.problems


def test_short_and_everyday_names_only_match_code_like_contexts(tmp_path):
    result = import_radar("name,ring\nGo,hold\nRust,trial\nDjango,trial\n")
    out = tmp_path / "radar.toml"
    out.write_text(result.toml, encoding="utf-8")
    radar = load_radar(out)
    assert radar.get("go").text_strict and radar.get("rust").text_strict
    assert not radar.get("django").text_strict
    assert radar.scan_text("Let users go to their history page") == []


def test_a_french_excel_export_in_cp1252_is_read(cli_root, tmp_path, capsys):
    source = tmp_path / "radar.csv"
    source.write_bytes("Nom;Anneau\nÉlasticsearch;évaluer\n".encode("cp1252"))
    assert main(["radar-import", str(source), "--out", str(tmp_path / "out.toml")]) == 0
    assert "Élasticsearch" in (tmp_path / "out.toml").read_text(encoding="utf-8")


def test_a_missing_radar_export_is_a_clear_error(cli_root, tmp_path, capsys):
    assert main(["radar-import", str(tmp_path / "nope.csv")]) == 2
    assert "cannot read" in capsys.readouterr().err


# ------------------------------------------------------------------ keyword triage


def test_a_negated_need_is_not_a_need():
    assert optional("API only, no UI, nothing stored") == set()
    assert optional("A dashboard of orders, without a database") == {"frontend"}
    assert optional("Track the orders but don't send any event") == {"database"}


def test_french_j_ai_is_not_ai_but_capital_ai_is():
    assert "ai" not in optional("j'ai besoin d'une liste des commandes")
    assert "ai" in optional("Use AI to summarise each ticket")
    assert "ai" in optional("an LLM answers the question")
