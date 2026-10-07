from pathlib import Path

import pytest

from factory.cli import main
from factory.demo import DemoError, expect, run_demo

REPO = Path(__file__).resolve().parents[1]


def test_demo_runs_offline_and_tells_the_story():
    lines: list[str] = []
    run_demo(REPO, say=lines.append)
    out = "\n".join(lines)
    assert "DEMO OK" in out
    assert "MongoDB (hold): not allowed, will use PostgreSQL" in out
    assert "BLOCKED [block] Flask (hold)" in out
    assert "exception recorded: ['django']" in out


def test_demo_does_not_touch_the_real_workspace():
    before = {p.name for p in REPO.iterdir()}
    run_demo(REPO, say=lambda _: None)
    assert {p.name for p in REPO.iterdir()} == before  # no work/ or apps/ created


def test_demo_fails_loudly_when_the_radar_is_weakened(tmp_path):
    """If IT removed Flask from hold, the demo's claim would be false: it must say so, not pass."""
    import shutil

    for name in ("factory.toml", "radar.toml"):
        shutil.copy2(REPO / name, tmp_path / name)
    shutil.copytree(REPO / "golden_paths", tmp_path / "golden_paths")
    radar = tmp_path / "radar.toml"
    radar.write_text(
        radar.read_text(encoding="utf-8").replace(
            'ring = "hold"\nmatch = ["flask"]', 'ring = "adopt"\nmatch = ["flask"]'
        ),
        encoding="utf-8",
    )
    with pytest.raises(DemoError, match="radar guard missed"):
        run_demo(tmp_path, say=lambda _: None)


def test_cli_demo_exit_codes(capsys, monkeypatch):
    monkeypatch.setenv("AI_FACTORY_ROOT", str(REPO))
    assert main(["demo"]) == 0
    assert "DEMO OK" in capsys.readouterr().out


def test_expect_helper():
    expect(True, "fine")
    with pytest.raises(DemoError):
        expect(False, "nope")
