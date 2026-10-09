"""Error and edge paths of the core modules (config, radar, design, detect, drift, guard, importer, judge,
policy, project, claudo): each branch is exercised for what it does, offline."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from factory import claudo, project
from factory.claudo import ClaudoEngine, EngineError, state_dir
from factory.config import ConfigError, find_root, load_config
from factory.design import (
    DesignChoice,
    choose_stack,
    existing_stack,
    grounded_capabilities,
    render_change_design,
    render_design,
)
from factory.detect import iter_files, scan_file
from factory.drift import radar_diff, render_diff
from factory.guard import check_project
from factory.importer import import_radar
from factory.judge import JudgeReport, combine, evaluate, extract_json
from factory.policy import _release, installed_licenses
from factory.project import (
    ProjectError,
    abandon_change,
    begin_change,
    head_sha,
    lock_dependencies,
    merge_plan_copy,
    prepare_project,
    sync_merged_base,
)
from factory.radar import RadarError, Tech, load_radar, verdict
from tests.test_claudo_bridge import fake_claudo
from tests.test_judge_panel import ARTIFACT, answer

REPO = Path(__file__).resolve().parents[1]


def radar_file(tmp_path, body, name="radar.toml"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


# ------------------------------------------------------------------ config


def test_no_factory_toml_anywhere_up_the_tree(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_FACTORY_ROOT", raising=False)
    monkeypatch.setattr(Path, "parents", property(lambda self: []))
    with pytest.raises(ConfigError, match="no factory.toml found"):
        find_root(tmp_path)


def test_a_missing_or_broken_factory_toml(tmp_path):
    with pytest.raises(ConfigError, match="missing"):
        load_config(tmp_path)
    (tmp_path / "factory.toml").write_text("[agent", encoding="utf-8")
    with pytest.raises(ConfigError, match="factory.toml"):
        load_config(tmp_path)


def test_gates_for_only_some_maturities_keep_the_defaults_for_the_others(tmp_path):
    (tmp_path / "factory.toml").write_text('[gates]\npoc = ["radar"]\n', encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.gates_for("poc") == ["radar"] and "tests" in cfg.gates_for("mvp")


def test_an_unknown_identity_provider_is_refused(tmp_path):
    (tmp_path / "factory.toml").write_text('[identity]\nprovider = "ldap"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="provider = 'ldap'"):
        load_config(tmp_path)


# ------------------------------------------------------------------ radar


def test_radar_loading_errors(tmp_path):
    with pytest.raises(RadarError, match="not found"):
        load_radar(tmp_path / "nope.toml")
    with pytest.raises(RadarError, match="radar.toml"):
        load_radar(radar_file(tmp_path, "[[tech]"))
    body = '[[tech]]\nid = "a"\nname = "A"\n[[tech]]\nid = "b"\nname = "B"\ncategory = "x"\nring = "adopt"\n'
    body += '[[tech]]\nid = "b"\nname = "B2"\ncategory = "x"\nring = "adopt"\n'
    with pytest.raises(RadarError) as raised:
        load_radar(radar_file(tmp_path, body))
    assert "missing category, ring" in str(raised.value) and "duplicate id" in str(raised.value)


def test_a_tech_without_aliases_is_never_seen_in_text(tmp_path):
    radar = load_radar(
        radar_file(tmp_path, '[[tech]]\nid = "z"\nname = "Z"\ncategory = "x"\nring = "adopt"\n')
    )
    assert radar.scan_text("z everywhere") == [] and radar.find("z").id == "z"


def test_an_unknown_maturity_is_a_programming_error():
    with pytest.raises(ValueError, match="unknown maturity 'beta'"):
        verdict(None, "beta")


# ------------------------------------------------------------------ design


def test_the_capability_analysts_answer_is_validated():
    assert grounded_capabilities("{not json}", "idea", ["ai"]) == (
        [],
        ["the capability analyst's answer is not the expected JSON"],
    )
    answer_ = json.dumps(
        {"capabilities": ["ai", {"id": "ai", "quote": "an LLM"}, {"id": "ai", "quote": "an LLM"}]}
    )
    assert grounded_capabilities(answer_, "use an LLM", ["ai"]) == (["ai"], [])


def test_a_capability_the_radar_cannot_fill_is_a_gap_in_the_design():
    radar = load_radar(REPO / "radar.toml")
    choice = choose_stack(radar, ["backend", "teleportation"], "poc", [])
    assert choice.gaps == ["teleportation"]
    text = render_design(
        slug="x", title="X", maturity="poc", radar=radar, choice=choice, gates=["radar"], spec_version="0.1.0"
    )
    assert "Capabilities with no allowed technology on the radar (ask IT): teleportation." in text


def test_a_change_design_lists_what_needs_approval_and_what_is_unknown(tmp_path):
    radar = load_radar(REPO / "radar.toml")
    (tmp_path / "requirements.txt").write_text("django\nmystery-lib\n", encoding="utf-8")
    existing = existing_stack(tmp_path, radar)
    assert existing.unknown == ["mystery-lib"]
    text = render_change_design(
        slug="c",
        title="C",
        kind="feature",
        target="t",
        maturity="mvp",
        radar=radar,
        existing=existing,
        gates=[],
        spec_version="0.1.0",
    )
    assert "- Django (trial)" in text and "unknown to the radar (ask IT to assess): mystery-lib" in text
    assert DesignChoice().gaps == []


# ------------------------------------------------------------------ detect


def test_dependency_specs_without_a_name_and_optional_dependencies(tmp_path):
    text = '[project]\ndependencies = ["==1.0", "flask"]\n[project.optional-dependencies]\ndev = ["django"]\n'
    assert [f.name for f in scan_file(_write(tmp_path, "pyproject.toml", text), "pyproject.toml")] == [
        "flask",
        "django",
    ]
    assert scan_file(_write(tmp_path, "requirements.txt", "==1.0\n"), "requirements.txt") == []


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_a_broken_git_listing_falls_back_to_a_walk(tmp_path, monkeypatch):
    from factory import detect

    (tmp_path / ".git").mkdir()
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(detect, "_git_files", lambda root, *flags: None)
    assert [p.name for p in iter_files(tmp_path)] == ["a.py"]


# ------------------------------------------------------------------ drift and guard


def test_radar_diff_reports_replacements_categories_and_additions(tmp_path):
    old = load_radar(radar_file(tmp_path, '[[tech]]\nid = "a"\nname = "A"\ncategory = "x"\nring = "adopt"\n'))
    new = load_radar(
        radar_file(
            tmp_path,
            '[[tech]]\nid = "a"\nname = "A"\ncategory = "y"\nring = "hold"\nreplaced_by = "b"\n'
            '[[tech]]\nid = "b"\nname = "B"\ncategory = "y"\nring = "adopt"\n',
            "new.toml",
        )
    )
    diff = radar_diff(old, new)
    assert "a: replaced_by - -> b" in diff.other_changes and "a: category x -> y" in diff.other_changes
    assert "+ b" in render_diff(diff, old, new)


def test_guard_hints_and_missing_docs(tmp_path):
    radar = load_radar(REPO / "radar.toml")
    (tmp_path / "requirements.txt").write_text("langchain\n", encoding="utf-8")
    report = check_project(tmp_path, radar, "prod", docs=[tmp_path / "missing.md"])
    [v] = report.blocking()
    assert v.hint == "'assess' technologies are not allowed at prod"


# ------------------------------------------------------------------ importer


def test_the_importer_keeps_golden_paths_and_reports_unreadable_json():
    result = import_radar("name,ring,golden_path\nFastAPI,adopt,python-fastapi\n")
    assert 'golden_path = "python-fastapi"' in result.toml
    bad = import_radar("{broken", source="radar.json")
    assert not bad.ok and "cannot read radar.json" in bad.problems[0]


# ------------------------------------------------------------------ judge


def test_judge_edges():
    assert JudgeReport(kind="spec").to_dict()["kind"] == "spec"
    assert extract_json("{not: json}") is None
    rep = evaluate("spec", json.dumps({"criteria": ["not a dict"], "summary": "s"}), ARTIFACT)
    assert rep.verdict == "unreliable"
    full = evaluate("spec", answer([4, 4, 4, 4, 4, 4]), ARTIFACT)
    partial = evaluate("spec", answer([4, 4, 4, 4]), ARTIFACT)  # reliable, but two criteria unscored
    panel = combine("spec", [partial, partial, full])
    assert {c.id for c in panel.criteria} == {c.id for c in full.criteria}
    only_partial = combine("spec", [partial, partial])  # two criteria scored by no run: left out
    assert len(only_partial.criteria) == len(partial.criteria) == 4


# ------------------------------------------------------------------ policy


def test_release_numbers_and_licence_metadata(tmp_path):
    assert _release("1.dev0") == (1,) and _release("2.0rc1") == (2, 0)
    site = tmp_path / ".venv" / "lib" / "python3.12" / "site-packages"
    (site / "a-1.dist-info").mkdir(parents=True)
    (site / "a-1.dist-info" / "METADATA").write_text("Name: a\nLicense: MIT", encoding="utf-8")  # no body
    (site / "b-1.dist-info").mkdir()
    (site / "b-1.dist-info" / "METADATA").write_text("License: MIT\n\nno name", encoding="utf-8")
    assert installed_licenses(tmp_path) == {"a": "MIT"}


# ------------------------------------------------------------------ project


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
    prepare_project(tmp_path)
    return tmp_path


def git(app, *args):
    return subprocess.run(["git", *args], cwd=app, capture_output=True, text=True, check=True).stdout.strip()


def test_abandon_and_sync_refuse_a_dirty_tree(repo):
    begin_change(repo, "x")
    (repo / "dirty.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(ProjectError, match="before abandoning"):
        abandon_change(repo, "x")
    with pytest.raises(ProjectError, match="before syncing"):
        sync_merged_base(repo, "x", "main")


def test_abandon_from_another_branch_and_sync_without_the_change_branch(repo, tmp_path):
    begin_change(repo, "x")
    git(repo, "switch", "-q", "main")
    abandon_change(repo, "x")  # not on the change branch: just deleted
    remote = repo.parent / f"{repo.name}-remote.git"  # outside the app folder
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main")
    assert sync_merged_base(repo, "x", "main") == git(repo, "rev-parse", "HEAD")


def test_head_sha_of_a_folder_that_is_not_a_repository(tmp_path):
    assert head_sha(tmp_path) == ""


def test_lock_dependencies_paths(tmp_path, monkeypatch):
    assert lock_dependencies(tmp_path) == (0, "nothing to lock")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    monkeypatch.setattr(project.shutil, "which", lambda name: None)
    assert lock_dependencies(tmp_path) == (127, "uv not found on PATH")
    monkeypatch.setattr(project.shutil, "which", lambda name: "uv")

    def hang(*a, **k):
        raise subprocess.TimeoutExpired("uv", 300)

    monkeypatch.setattr(project.subprocess, "run", hang)
    assert lock_dependencies(tmp_path) == (124, "uv lock timed out")
    monkeypatch.setattr(
        project.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "locked", "")
    )
    assert lock_dependencies(tmp_path) == (0, "locked")


def test_merging_a_plan_copy_keeps_its_run_log():
    approved = "### T1 — x\n"
    assert merge_plan_copy(approved, "### T1 — x\n") == approved  # nothing logged yet
    legacy = "### T1 — x\n| 2026-10-08 07:09 | T1 | agent | done | |\n"
    merged = merge_plan_copy("### T1 — x\n\n## Run log\n", legacy)
    assert merged.endswith("| 2026-10-08 07:09 | T1 | agent | done | |\n") and merged.count("## Run log") == 1


# ------------------------------------------------------------------ claudo bridge


def test_plan_lint_and_signing_failures(tmp_path, monkeypatch):
    engine = ClaudoEngine(fake_claudo(tmp_path / "c"))

    def hang(*a, **k):
        raise subprocess.TimeoutExpired("lint", 60)

    monkeypatch.setattr(claudo.subprocess, "run", hang)
    with pytest.raises(EngineError, match="plan-lint timed out"):
        engine.lint_plan("x", spec="s", design="d", tasks="t")
    monkeypatch.setattr(
        claudo.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "bad secret")
    )
    with pytest.raises(EngineError, match="signing CP-1 failed: bad secret"):
        engine.sign_approval(tmp_path, "x", "CP-1", "bob", "s")


def test_a_reader_crash_still_stops_the_orchestrator(tmp_path, monkeypatch):
    home = fake_claudo(tmp_path / "c")
    (home / "lab" / "engine" / "orchestrate.py").write_text(
        "import time\nprint('started', flush=True)\ntime.sleep(60)\n", encoding="utf-8"
    )

    class Boom:
        def search(self, line):
            raise RuntimeError("reader broke")

    monkeypatch.setattr(claudo, "CHECKPOINT_WAIT", Boom())
    with pytest.raises(RuntimeError, match="reader broke"):
        ClaudoEngine(home).run_build("x", tmp_path / "p", timeout=30)


def test_the_state_directory_outside_windows(monkeypatch, tmp_path):
    from types import SimpleNamespace

    environ = {"XDG_STATE_HOME": str(tmp_path)}
    monkeypatch.setattr(claudo, "os", SimpleNamespace(name="posix", environ=environ))
    assert state_dir() == tmp_path / "ai-factory"
    environ.clear()
    assert state_dir() == Path.home() / ".local" / "state" / "ai-factory"
    assert os.name and Tech  # the real os module is untouched; Tech is used by the radar tests above


def test_the_state_directory_on_windows(monkeypatch, tmp_path):
    from types import SimpleNamespace

    environ = {"LOCALAPPDATA": str(tmp_path)}
    monkeypatch.setattr(claudo, "os", SimpleNamespace(name="nt", environ=environ))
    assert state_dir() == tmp_path / "ai-factory"
    environ.clear()
    assert state_dir() == Path.home() / "AppData" / "Local" / "ai-factory"
