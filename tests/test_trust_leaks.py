"""Smaller trust leaks next to the sandbox boundary (H1.e: audit A65, A90, A126-A128, credentials in argv)."""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from factory import sandbox as sb
from factory.config import ConfigError, find_root
from factory.foreman import JUDGE_GROUP_BUDGET, judged_sources
from factory.sandbox import Sandbox, SandboxedRunner

# ------------------------------------------------------------------ credentials and orphaned containers


@pytest.fixture
def credential(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oat-" + "value")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_gate_containers_get_no_model_credential(monkeypatch, tmp_path, credential):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    Sandbox(git_modes=False).executor("uv run pytest", tmp_path)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in " ".join(seen["argv"])
    assert seen["argv"][seen["argv"].index("--name") + 1].startswith("factory-gate-")


def test_a_gate_timeout_kills_the_container_not_only_the_cli(monkeypatch, tmp_path):
    killed = []

    def hang(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 5)

    monkeypatch.setattr(sb.subprocess, "run", hang)
    box = Sandbox(git_modes=False, docker=lambda a: killed.append(a) or (0, ""), timeout=5)
    rc, out = box.executor("sleep 99", tmp_path)
    assert rc == 124 and "timeout: sleep 99" in out and "killed" in out
    assert killed[0][0] == "kill" and killed[0][1].startswith("factory-gate-")


def test_an_agent_timeout_kills_its_container_and_fails_the_run(monkeypatch, tmp_path, credential):
    killed = []

    def hang(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 5)

    monkeypatch.setattr(sb.subprocess, "run", hang)
    box = Sandbox(docker=lambda a: killed.append(a) or (0, ""))
    result = SandboxedRunner(box, timeout=5).run("p", cwd=tmp_path)
    assert not result.ok and "timed out after 5s" in result.error
    assert killed[0][1].startswith("factory-agent-")


def test_without_docker_the_sandboxed_agent_fails_clearly(monkeypatch, tmp_path):
    def missing(argv, **kw):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(sb.subprocess, "run", missing)
    result = SandboxedRunner(Sandbox()).run("p", cwd=tmp_path)
    assert not result.ok and "docker not found" in result.error
    assert Sandbox(git_modes=False).executor("true", tmp_path) == (
        127,
        "docker not found: the sandbox needs Docker",
    )


# ------------------------------------------------------------------ what the build judge reads


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_the_judge_reads_backend_frontend_and_tests_each_with_its_own_budget(tmp_path):
    write(tmp_path / "app" / "big.py", "x" * (JUDGE_GROUP_BUDGET + 10))
    write(tmp_path / "app" / "zz_more.py", "y = 1\n")
    write(tmp_path / "web" / "src" / "App.tsx", "export const A = 1;\n")
    write(tmp_path / "tests" / "test_a.py", "def test_a(): pass\n")
    text = judged_sources(tmp_path)
    assert "### web/src/App.tsx" in text and "### tests/test_a.py" in text
    assert "### app/: 1 more file(s) not shown (budget)" in text and "zz_more" not in text


def test_the_judge_never_follows_a_symlink_out_of_the_app(tmp_path):
    secret = tmp_path / "outside" / "secret.py"
    write(secret, "SECRET = 'host file'\n")
    app = tmp_path / "app_root"
    write(app / "app" / "main.py", "x = 1\n")
    try:
        (app / "app" / "leak.py").symlink_to(secret)
    except OSError:
        pytest.skip("symlinks need privileges on this Windows host")
    assert "host file" not in judged_sources(app)


# ------------------------------------------------------------------ which factory.toml is the factory's


def test_an_apps_own_factory_toml_is_never_taken_as_the_factorys(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_FACTORY_ROOT", raising=False)
    (tmp_path / "factory.toml").write_text("[factory]\n", encoding="utf-8")
    inner = tmp_path / "apps" / "orders"
    inner.mkdir(parents=True)
    (inner / "factory.toml").write_text("[factory]\n", encoding="utf-8")  # written by an agent
    with pytest.raises(ConfigError, match="belongs to an app"):
        find_root(inner / "app")


def test_a_custom_apps_dir_is_honoured_and_a_broken_outer_config_ignored(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_FACTORY_ROOT", raising=False)
    (tmp_path / "factory.toml").write_text('[factory]\napps_dir = "built"\n', encoding="utf-8")
    (tmp_path / "built" / "x").mkdir(parents=True)
    (tmp_path / "built" / "x" / "factory.toml").write_text("", encoding="utf-8")
    with pytest.raises(ConfigError):
        find_root(tmp_path / "built" / "x")
    nested = tmp_path / "other"
    nested.mkdir()
    (nested / "factory.toml").write_text("", encoding="utf-8")
    assert find_root(nested) == nested.resolve()  # not under apps: a factory of its own
    (tmp_path / "factory.toml").write_text("[broken", encoding="utf-8")
    assert find_root(nested) == nested.resolve()


def test_the_environment_variable_still_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_FACTORY_ROOT", str(tmp_path))
    assert find_root(Path(os.sep)) == tmp_path.resolve()
