from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from factory.config import load_config
from factory.foreman import Foreman
from factory.radar import load_radar
from factory.workitem import Store

REPO = Path(__file__).resolve().parents[1]


class FakeExecutor:
    """Stands in for shell commands (tests / lint gates): no network, no uv."""

    def __init__(self, rc: int = 0, out: str = "ok") -> None:
        self.rc, self.out, self.calls = rc, out, []

    def __call__(self, command: str, cwd: Path) -> tuple[int, str]:
        self.calls.append((command, cwd))
        return self.rc, self.out


class ScriptedExecutor:
    """Command gate results in order (last one repeats): e.g. fail, fail, pass."""

    def __init__(self, results: list[tuple[int, str]]) -> None:
        self.results, self.calls = list(results), 0

    def __call__(self, command: str, cwd: Path) -> tuple[int, str]:
        res = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return res


class FakeAgentRunner:
    """Stands in for ClaudeRunner: records prompts, never touches the network."""

    name = "claude"

    def __init__(self, text: str = "built it") -> None:
        self.text, self.prompts = text, []

    def run(self, prompt: str, **kw):
        from factory.agents import AgentResult

        self.prompts.append((prompt, kw))
        return AgentResult(True, self.text, 0.01)


@pytest.fixture
def factory_root(tmp_path: Path) -> Path:
    """A throwaway factory: the real factory.toml, radar and golden paths, empty work/apps."""
    for name in ("factory.toml", "radar.toml"):
        shutil.copy2(REPO / name, tmp_path / name)
    shutil.copytree(REPO / "golden_paths", tmp_path / "golden_paths")
    return tmp_path


@pytest.fixture
def radar():
    return load_radar(REPO / "radar.toml")


@pytest.fixture
def executor() -> FakeExecutor:
    return FakeExecutor()


@pytest.fixture
def foreman(factory_root: Path, executor: FakeExecutor) -> Foreman:
    cfg = load_config(factory_root)
    return Foreman(cfg, load_radar(cfg.radar_path), Store(cfg.work_dir), runner=None, executor=executor)
