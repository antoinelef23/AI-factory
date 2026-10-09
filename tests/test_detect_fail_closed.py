"""The radar sees every dependency declaration, or says it cannot (H5: A20-A24, A98-A100, A102, A103)."""

import pytest

from factory.detect import scan_file, scan_project
from factory.guard import check_project


def findings(tmp_path, rel, text):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return [(f.name, f.kind) for f in scan_file(path, rel)]


def test_requirements_includes_and_editables(tmp_path):
    text = (
        "-r base.txt\n-c constraints.txt\nflask==3.0  # web\n-e git+https://x/y.git#egg=mylib\n-e ./local\n"
    )
    assert findings(tmp_path, "requirements/dev.txt", text) == [
        ("flask", "manifest"),
        ("mylib", "manifest"),
        ("editable install -e ./local", "unanalysed"),
    ]


@pytest.mark.parametrize("rel", ["requirements/base.txt", "constraints.txt", "requirements-dev.txt"])
def test_every_requirements_file_is_read(tmp_path, rel):
    assert findings(tmp_path, rel, "django\n") == [("django", "manifest")]


def test_an_unreadable_requirements_file_in_a_folder_is_an_error(tmp_path):
    path = tmp_path / "requirements" / "base.txt"
    path.parent.mkdir()
    path.write_bytes(b"\xff\xfe\xfa")
    assert [f.kind for f in scan_file(path, "requirements/base.txt")] == ["error"]


def test_uv_and_pdm_dev_dependencies(tmp_path):
    text = (
        '[project]\nname = "x"\ndependencies = []\n[tool.uv]\ndev-dependencies = ["flask>=3"]\n'
        '[tool.pdm.dev-dependencies]\nlint = ["django"]\n'
    )
    assert sorted(findings(tmp_path, "pyproject.toml", text)) == [
        ("django", "manifest"),
        ("flask", "manifest"),
    ]


def test_package_json_optional_and_bundled(tmp_path):
    text = (
        '{"optionalDependencies": {"angular": "1"}, "bundledDependencies": ["left-pad"], '
        '"bundleDependencies": true}'
    )
    assert findings(tmp_path, "package.json", text) == [("angular", "manifest"), ("left-pad", "manifest")]


@pytest.mark.parametrize("name", ["Pipfile", "setup.py", "setup.cfg", "environment.yml"])
def test_python_manifests_the_radar_does_not_parse_are_reported(tmp_path, name):
    assert findings(tmp_path, name, "anything\n") == [(name, "unanalysed")]


def test_multi_stage_dockerfile_aliases_and_args(tmp_path):
    text = (
        "ARG BASE=python:3.12-slim\nARG UNSET\n"
        "FROM ${BASE} AS builder\nRUN make\n"
        "FROM builder AS test\n"
        "FROM node:24-slim\n"
        "COPY --from=builder /app /app\nCOPY --from=0 /x /x\n"
        "COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv\n"
        "FROM $UNSET\nFROM scratch\n"
    )
    assert findings(tmp_path, "Dockerfile", text) == [
        ("python", "image"),
        ("node", "image"),
        ("uv", "image"),
        ("image $UNSET (an unresolved build argument)", "unanalysed"),
    ]


def test_a_standard_two_stage_dockerfile_passes_prod(tmp_path, radar):
    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.12-slim AS builder\nRUN pip wheel .\n"
        "FROM python:3.12-slim\nCOPY --from=builder /w /w\n",
        encoding="utf-8",
    )
    report = check_project(tmp_path, radar, "prod")
    assert not [v for v in report.blocking() if "builder" in v.key]


def test_python_imports_are_read_with_the_parser(tmp_path):
    text = "import os, flask\nfrom . import local\nfrom django.db import models\nimport a.b as c\n"
    assert findings(tmp_path, "app/x.py", text) == [
        ("a", "import"),
        ("django", "import"),
        ("flask", "import"),
        ("os", "import"),
    ]


def test_a_python_file_that_does_not_parse_still_has_its_imports_read(tmp_path):
    assert findings(tmp_path, "app/x.py", "import flask\ndef broken(:\n") == [("flask", "import")]


def test_the_scan_of_a_whole_project_sees_a_held_dev_dependency(tmp_path, radar):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n[tool.uv]\ndev-dependencies = ["motor"]\n', encoding="utf-8"
    )
    assert ("motor", "pyproject.toml") in [(f.name, f.source) for f in scan_project(tmp_path)]
    assert any(v.key == "mongodb" for v in check_project(tmp_path, radar, "prod").blocking())
