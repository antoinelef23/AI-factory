"""More golden paths, chosen from manifests (ROADMAP P3-8)."""

import shutil
from pathlib import Path

import pytest

from factory.design import load_golden_paths, pick_golden_path
from factory.foreman import golden_gate_commands
from factory.guard import check_project
from factory.radar import load_radar
from tests.test_drift import radar_with

REPO = Path(__file__).resolve().parents[1]
GOLDEN = REPO / "golden_paths"
BASE = ["language", "backend", "testing", "quality", "ci", "hosting"]


@pytest.fixture
def radar():
    return load_radar(REPO / "radar.toml")


def test_the_three_golden_paths_have_manifests():
    paths = {gp.name: gp for gp in load_golden_paths(GOLDEN)}
    assert set(paths) == {"python-fastapi", "fullstack-react", "python-worker"}
    assert (
        "frontend" in paths["fullstack-react"].capabilities
        and "messaging" in paths["python-worker"].capabilities
    )
    assert (
        paths["fullstack-react"]
        .gate_commands["tests"]
        .startswith("uv run --quiet pytest -q && npm --prefix web")
    )


@pytest.mark.parametrize("name", ["python-fastapi", "fullstack-react", "python-worker"])
def test_every_golden_path_passes_the_radar_at_prod(radar, name):
    report = check_project(GOLDEN / name, radar, "prod")
    assert report.ok(), [v.describe() for v in report.violations]


@pytest.mark.parametrize(
    ("needs", "expected"),
    [
        ([], "python-fastapi"),
        (["database"], "python-fastapi"),
        (["frontend", "database"], "fullstack-react"),
        (["messaging"], "python-worker"),
        (["frontend", "messaging"], "fullstack-react"),  # higher priority wins a tie
    ],
)
def test_the_template_follows_what_the_idea_needs(radar, needs, expected):
    chosen = pick_golden_path(load_golden_paths(GOLDEN), radar, BASE + needs, "mvp")
    assert chosen.name == expected


def test_a_template_using_a_blocked_technology_is_not_offered(tmp_path):
    radar, _ = radar_with(tmp_path, {"react": "hold"})
    chosen = pick_golden_path(load_golden_paths(GOLDEN), radar, [*BASE, "frontend"], "mvp")
    assert chosen.name == "python-fastapi"  # the frontend stays a design gap for IT, not a forbidden template


def test_without_manifests_the_radar_golden_path_is_used(foreman, tmp_path):
    legacy = tmp_path / "legacy"
    shutil.copytree(GOLDEN / "python-fastapi", legacy / "python-fastapi")
    (legacy / "python-fastapi" / "golden.toml").unlink()
    foreman.cfg.golden_paths_dir = legacy
    item = foreman.intake("X", "an api", "poc")
    assert foreman._golden_path(item) == "python-fastapi"


def test_a_frontend_idea_is_scaffolded_from_fullstack_react_with_its_placeholders(foreman):
    item = foreman.approve(
        foreman.run(foreman.intake("Visitor log", "A page listing visitors", "poc")), "business"
    )
    item = foreman.approve(item, "owner")
    app = foreman.app_dir(item)
    assert (app / "web" / "src" / "App.tsx").is_file() and (app / "app" / "main.py").is_file()
    assert 'const TITLE = "Visitor log";' in (app / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "<title>Visitor log</title>" in (app / "web" / "index.html").read_text(encoding="utf-8")
    assert '"name": "visitor-log-web"' in (app / "web" / "package-lock.json").read_text(encoding="utf-8")
    assert "{{" not in (app / "web" / "package.json").read_text(encoding="utf-8")
    assert item.golden_path == "fullstack-react"
    assert foreman._gate_commands(item)["tests"].endswith("npm --prefix web test")


def test_a_messaging_idea_is_scaffolded_from_python_worker(foreman):
    item = foreman.run(foreman.intake("Order events", "Consume order events from a queue", "poc"))
    item = foreman.approve(foreman.approve(item, "business"), "owner")
    app = foreman.app_dir(item)
    assert (app / "worker" / "__init__.py").is_file() and (app / "app" / "main.py").is_file()
    assert item.golden_path == "python-worker"
    assert foreman._gate_commands(item) == {}  # the factory's own gate commands apply


def test_an_app_without_a_manifest_has_no_gate_overrides(tmp_path):
    assert golden_gate_commands(tmp_path / "golden.toml") == {}


@pytest.mark.parametrize("folder", ["python-fastapi", "python-worker", "fullstack-react"])
def test_a_manifest_lists_every_radar_technology_its_template_ships(radar, folder):
    """A held technology must stop the template from being offered: so `techs` cannot omit one (A80)."""
    [gp] = [g for g in load_golden_paths(GOLDEN) if g.folder == folder]
    shipped = set(check_project(GOLDEN / folder, radar, "prod").allowed)
    assert shipped <= set(gp.techs), f"{folder} ships {sorted(shipped - set(gp.techs))} not in its techs"
    assert all(radar.get(t) for t in gp.techs)


def test_the_images_are_pinned_locked_and_unprivileged():
    for folder in ("python-fastapi", "python-worker", "fullstack-react"):
        text = (GOLDEN / folder / "Dockerfile").read_text(encoding="utf-8")
        froms = [ln.split()[1] for ln in text.splitlines() if ln.startswith(("FROM ", "COPY --from=ghcr"))]
        assert froms and all("@sha256:" in ref or ref.startswith("--from=ghcr") for ref in froms), folder
        assert "uv:latest" not in text and "@sha256:" in text.split("COPY --from=ghcr", 1)[1].split()[0]
        assert "uv sync --locked --no-dev" in text and "\nUSER app\n" in text
        assert (GOLDEN / folder / ".dockerignore").is_file()
    assert "COPY worker ./worker" in (GOLDEN / "python-worker" / "Dockerfile").read_text(encoding="utf-8")


def test_the_scaffold_leaves_local_environments_behind_and_copies_binaries(foreman):
    template = foreman.cfg.golden_paths_dir / "python-fastapi"
    (template / ".venv" / "lib").mkdir(parents=True)
    (template / ".venv" / "lib" / "x.py").write_text("junk\n", encoding="utf-8")
    (template / "web" / "node_modules").mkdir(parents=True)
    (template / "logo.json").write_bytes(b"\xff\xfe\x00binary")  # a text suffix, binary content
    item = foreman.run(foreman.intake("Order api", "An HTTP API that lists orders", "poc"))
    item = foreman.approve(foreman.approve(item, "business"), "owner")
    app = foreman.app_dir(item)
    assert not (app / ".venv").exists() and not (app / "web" / "node_modules").exists()
    assert (app / "logo.json").read_bytes() == b"\xff\xfe\x00binary"
