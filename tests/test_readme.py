"""The README is executable documentation: its offline walkthrough runs as written and its commands exist.

Docs rot silently; this makes drift between README and code a failing test instead of a surprise on someone's
first evening with the tool.
"""

import re
import shlex
from pathlib import Path

import pytest

from factory.cli import build_parser, main

README = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")


def code_blocks(section_start: str, section_end: str) -> list[list[str]]:
    """The powershell code blocks between two headings, as lists of command lines (comments stripped)."""
    body = README.split(section_start, 1)[1].split(section_end, 1)[0]
    blocks = re.findall(r"```powershell\n(.*?)```", body, re.S)
    return [[ln.split("   #")[0].rstrip() for ln in b.splitlines() if ln.strip()] for b in blocks]


def factory_argv(line: str) -> list[str] | None:
    """`uv run factory radar --x` -> ["radar", "--x"]; None for any other command (cd, python...)."""
    match = re.match(r"uv run factory\s+(.*)", line.strip())
    return shlex.split(match.group(1)) if match else None


@pytest.fixture
def factory(factory_root, monkeypatch):
    monkeypatch.setenv("AI_FACTORY_ROOT", str(factory_root))
    toml = factory_root / "factory.toml"
    # The offline run builds an app: skip the gate that shells out to `uv run pytest` (needs the network)
    toml.write_text(toml.read_text(encoding="utf-8").replace(', "tests"', "", 1), encoding="utf-8")
    return factory_root


def test_the_five_second_proof_runs_as_written(factory, capsys):
    [[line]] = code_blocks("### 0. The 5-second proof", "### 1. Offline run")
    assert main(factory_argv(line)) == 0
    assert "DEMO OK" in capsys.readouterr().out


def test_the_offline_walkthrough_runs_as_written_and_ships_the_app(factory, capsys):
    [lines] = code_blocks("### 1. Offline run", "### 2. Real agents")
    assert len(lines) >= 7
    for line in lines:
        argv = factory_argv(line)
        assert argv is not None, f"README step is not a factory command: {line}"
        assert main(argv) == 0, f"README step failed: {line}\n{capsys.readouterr().out}"
    out = capsys.readouterr().out
    assert "stage : shipped" in out and "MongoDB (hold): not allowed, will use PostgreSQL" in out
    assert (factory / "apps" / "customer-callback-log" / "app" / "main.py").is_file()
    assert (factory / "work" / "customer-callback-log" / "gate-report.md").is_file()


def test_every_factory_command_the_readme_mentions_exists():
    subcommands = set(build_parser()._subparsers._group_actions[0].choices)
    mentioned = set(re.findall(r"(?:uv run |^|`)factory ([a-z][a-z-]+)", README, re.M))
    assert mentioned, "the regex found no command: the test would pass vacuously"
    assert mentioned <= subcommands, f"README names unknown commands: {sorted(mentioned - subcommands)}"


def test_every_subcommand_is_documented_somewhere():
    subcommands = set(build_parser()._subparsers._group_actions[0].choices)
    undocumented = sorted(c for c in subcommands if f"factory {c}" not in README)
    assert undocumented == [], f"commands missing from the README: {undocumented}"


def test_the_files_the_readme_points_at_exist():
    root = Path(__file__).resolve().parents[1]
    for name in ("factory.toml", "radar.toml", "PROGRESS.md", "PLANS.md", "ROADMAP.md", "LICENSE"):
        assert (root / name).is_file(), name
    assert (root / "golden_paths" / "python-fastapi" / "pyproject.toml").is_file()


def test_every_subcommand_has_working_help_and_a_description():
    parser = build_parser()
    choices = parser._subparsers._group_actions[0]
    for name, sub in choices.choices.items():
        with pytest.raises(SystemExit) as e:
            main([name, "--help"])
        assert e.value.code == 0, name
        assert sub.format_usage().startswith("usage: factory"), name
    helps = {a.dest: a.help for a in choices._choices_actions}
    assert all(helps.get(n) for n in choices.choices), [n for n in choices.choices if not helps.get(n)]
