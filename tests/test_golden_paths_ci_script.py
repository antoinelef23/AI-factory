"""scripts/golden_paths_ci.py: what the root CI runs on every golden path (its commands are recorded here;
the CI job runs them for real, with docker)."""

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import golden_paths_ci as gp  # noqa: E402


@pytest.fixture
def recorded(monkeypatch):
    calls = []
    monkeypatch.setattr(gp, "run", lambda argv, cwd: calls.append((argv, cwd)))
    return calls


def test_every_template_is_scaffolded_and_checked(recorded, monkeypatch):
    monkeypatch.setattr("factory.foreman.lock_dependencies", lambda app: (0, "stub"))
    assert gp.main(["--docker"]) == 0
    commands = [" ".join(argv) for argv, _ in recorded]
    assert sum(c.startswith("uv run --quiet pytest") for c in commands) == 3
    assert sum(c.startswith("npm ci") for c in commands) == 1  # only fullstack-react has a frontend
    assert sum(c.startswith("docker build") for c in commands) == 3


def test_without_docker_no_image_is_built(recorded, monkeypatch):
    monkeypatch.setattr("factory.foreman.lock_dependencies", lambda app: (0, "stub"))
    assert gp.main(["python-fastapi"]) == 0
    assert not any(argv[0] == "docker" for argv, _ in recorded)


def test_a_failing_template_fails_the_run_and_is_named(monkeypatch, capsys):
    monkeypatch.setattr("factory.foreman.lock_dependencies", lambda app: (0, "stub"))

    def fail(argv, cwd):
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(gp, "run", fail)
    assert gp.main(["python-worker"]) == 1
    assert "FAILED: python-worker" in capsys.readouterr().out


def test_a_template_the_factory_would_not_pick_stops_the_check(tmp_path, monkeypatch):
    monkeypatch.setattr("factory.foreman.lock_dependencies", lambda app: (0, "stub"))
    monkeypatch.setitem(gp.IDEAS, "python-worker", ("Order api", "An HTTP API that lists orders"))
    import shutil

    for name in ("factory.toml", "radar.toml"):
        shutil.copy2(REPO / name, tmp_path / name)
    shutil.copytree(REPO / "golden_paths", tmp_path / "golden_paths")
    with pytest.raises(SystemExit, match="the factory picked 'python-fastapi'"):
        gp.scaffold(tmp_path, "python-worker")


def test_run_echoes_the_command_and_resolves_it_on_path(tmp_path, capsys):
    gp.run([sys.executable, "-c", "print('ok')"], tmp_path)
    assert capsys.readouterr().out.startswith("$ ")
