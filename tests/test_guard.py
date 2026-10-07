from pathlib import Path

from factory.detect import scan_project
from factory.gates import secrets_gate
from factory.guard import check_project


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_detects_manifests_images_and_imports(tmp_path):
    write(
        tmp_path,
        "pyproject.toml",
        '[project]\ndependencies = ["FastAPI>=0.1", "uvicorn[standard]"]\n'
        '[dependency-groups]\ndev = ["pytest"]\n',
    )
    write(tmp_path, "requirements.txt", "# comment\nrequests==2.0\n-e .\n")
    write(
        tmp_path,
        "package.json",
        '{"dependencies": {"react": "^18"}, "devDependencies": {"@types/node": "1"}}',
    )
    write(tmp_path, "Dockerfile", "FROM docker.io/library/python:3.12-slim AS base\n")
    write(tmp_path, "docker-compose.yml", "services:\n  db:\n    image: mongo:7\n")
    write(tmp_path, "app/x.py", "import os\nfrom sqlalchemy.orm import Session\n")
    write(
        tmp_path,
        "web/a.ts",
        "import x from 'react';\nimport y from './local';\nconst z = require('@scope/pkg/sub');\n",
    )
    write(tmp_path, ".venv/lib/flask.py", "import flask\n")  # skipped dir

    found = {(f.name, f.kind) for f in scan_project(tmp_path)}
    assert ("FastAPI", "manifest") in found
    assert ("uvicorn", "manifest") in found
    assert ("requests", "manifest") in found
    assert ("@types/node", "manifest") in found
    assert ("python", "image") in found
    assert ("mongo", "image") in found
    assert ("sqlalchemy", "import") in found
    assert ("@scope/pkg", "import") in found
    assert not any(n == "flask" for n, _ in found)
    assert not any(n == "./local" for n, _ in found)


def test_clean_golden_path_passes_in_prod(radar):
    gp = Path(__file__).resolve().parents[1] / "golden_paths" / "python-fastapi"
    report = check_project(gp, radar, "prod")
    assert report.ok(), [v.describe() for v in report.violations]
    assert {"fastapi", "pytest", "python", "httpx"} <= set(report.allowed)


def test_hold_tech_blocks_with_alternative(tmp_path, radar):
    write(tmp_path, "pyproject.toml", '[project]\ndependencies = ["pymongo"]\n')
    report = check_project(tmp_path, radar, "poc")
    [v] = report.blocking()
    assert v.key == "mongodb" and v.verdict == "block"
    assert "PostgreSQL" in v.hint


def test_unknown_dependency_needs_approval_and_exception_unblocks(tmp_path, radar):
    write(tmp_path, "pyproject.toml", '[project]\ndependencies = ["left-pad"]\n')
    report = check_project(tmp_path, radar, "mvp")
    assert [v.key for v in report.blocking()] == ["left-pad"]
    assert report.ok(exceptions={"left-pad"})
    assert not check_project(tmp_path, radar, "prod").ok(
        exceptions={"left-pad"}
    )  # prod: unknown always blocks


def test_unknown_imports_are_ignored(tmp_path, radar):
    write(tmp_path, "app/x.py", "import json\nimport mylocalmodule\n")
    assert check_project(tmp_path, radar, "prod").violations == []


def test_design_doc_ignore_block_is_not_usage(tmp_path, radar):
    write(
        tmp_path,
        "design.md",
        "Stack: FastAPI\n<!-- radar:ignore -->\nForbidden: Flask, MongoDB\n<!-- /radar:ignore -->\n",
    )
    report = check_project(tmp_path, radar, "prod", docs=[tmp_path / "design.md"])
    assert report.ok()
    write(tmp_path, "design.md", "Stack: FastAPI on MongoDB\n")
    assert not check_project(tmp_path, radar, "prod", docs=[tmp_path / "design.md"]).ok()


def test_secrets_gate(tmp_path):
    write(tmp_path, "app/ok.py", 'import os\nTOKEN = os.environ["TOKEN"]\n')
    assert secrets_gate(tmp_path).ok
    fake_key = (
        "sk-ant-" + "abcdefghijklmnopqrstuvwxyz123456"
    )  # assembled: never a token-shaped literal in source
    write(tmp_path, "app/bad.py", f'api_key = "{fake_key}"\n')
    result = secrets_gate(tmp_path)
    assert not result.ok
    assert "app/bad.py:1" in result.detail
