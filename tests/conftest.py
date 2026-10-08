from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from factory.config import load_config
from factory.foreman import Foreman
from factory.radar import load_radar
from factory.templates import offline_change_spec, offline_spec
from factory.workitem import Store, WorkItem

# Specs that pass the structural lint: what a well-behaved spec-writing agent answers. The factory's own
# offline templates are proven lint-clean (tests/test_spec_lint.py), so test doubles reuse them.
VALID_SPEC = offline_spec(WorkItem(slug="x", title="X", idea="an api", maturity="poc"))
VALID_CHANGE_SPEC = offline_change_spec(
    WorkItem(slug="y", title="Y", idea="change it", maturity="poc", kind="feature", target="x")
)

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
        if "spec writer" in prompt:
            return AgentResult(True, VALID_CHANGE_SPEC if "EXISTING app" in prompt else VALID_SPEC, 0.01)
        return AgentResult(True, self.text, 0.01)


class JudgingAgentRunner(FakeAgentRunner):
    """Answers judge prompts with valid, GROUNDED JSON (quotes the artifact's first line).

    `score` is what it gives every criterion; other prompts get a normal agent answer."""

    DOC = (
        "# Generated artifact\n\n"
        "- INV-1: every stored record MUST have a positive amount in EUR.\n"
        "- BHV-1: Given a valid request, When it is submitted, Then the response is 201.\n"
    )

    def __init__(self, score: int = 5, text: str = DOC) -> None:
        super().__init__(text)
        self.score = score

    def run(self, prompt: str, **kw):
        import json
        import re

        from factory.agents import AgentResult
        from factory.judge import RUBRICS

        if "independent reviewer" not in prompt:
            return super().run(prompt, **kw)
        self.prompts.append((prompt, kw))
        kind = re.search(r"reviewer of a (\w+) produced", prompt).group(1)
        artifact = re.search(r"<artifact>\n(.*?)\n</artifact>", prompt, re.S).group(1)
        # A meaningful excerpt (grounding needs >= 24 real characters): the longest line, capped.
        quote = max((ln.strip() for ln in artifact.splitlines()), key=len)[:100]
        crit = [
            {"id": c, "score": self.score, "evidence": f"{c} judged", "quote": quote}
            for c, _ in RUBRICS[kind]
        ]
        return AgentResult(True, json.dumps({"criteria": crit, "summary": "fake judge"}), 0.002)


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


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", help="run the tests that call a paid model")


def pytest_collection_modifyitems(config, items):
    """`live` tests call a paid model: they only run with --live (`just calibrate`) or FACTORY_LIVE=1."""
    import os

    if config.getoption("--live") or os.environ.get("FACTORY_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="billed: pass --live (just calibrate) to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path_factory, monkeypatch):
    """The approval secret lives in a per-user state dir: tests must never write to the real one."""
    monkeypatch.setenv("AI_FACTORY_STATE_DIR", str(tmp_path_factory.mktemp("factory-state")))
