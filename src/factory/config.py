"""Factory definition (factory.toml) loading and root discovery."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = "factory.toml"

DEFAULT_GATES = {
    "pov": ["radar", "secrets", "immutable"],
    "poc": ["radar", "secrets", "immutable", "tests"],
    "mvp": ["radar", "secrets", "immutable", "tests", "lint", "trajectory", "clean_tree"],
    "prod": ["radar", "secrets", "immutable", "tests", "lint", "trajectory", "clean_tree"],
}


class ConfigError(Exception):
    pass


@dataclass
class Config:
    root: Path
    name: str = "AI Software Factory"
    radar_path: Path = Path("radar.toml")
    work_dir: Path = Path("work")
    apps_dir: Path = Path("apps")
    golden_paths_dir: Path = Path("golden_paths")
    runner: str = "offline"
    max_turns_build: int = 40
    max_build_attempts: int = 3
    claudo_home: str | None = None  # [engine] claudo; None = auto-discover (CLAUDO_HOME, sibling checkout)
    plan_lint_retries: int = 2  # [engine] plan_lint_retries: re-prompts with Claudo's lint errors
    delivery_provider: str = "none"  # [delivery] provider: none | github
    delivery_owner: str = (
        ""  # [delivery] owner: account/org for the repositories ("" = the authenticated user)
    )
    repo_prefix: str = "app-"  # [delivery] repo_prefix: repository name = prefix + app slug
    claudo_build_from: str = "mvp"  # [engine] build_from: lowest maturity built through Claudo's orchestrator
    spec_lint_retries: int = 2  # [policy] spec_lint_retries: re-prompts with the structural lint's errors
    exception_days: int = (
        180  # [policy] exception_days: life of an exception granted at design review (0 = never)
    )
    claudo_timeout: int = 3600  # [engine] build_timeout: seconds per orchestrator run
    claudo_budget_usd: float = 0.0  # [engine] budget_usd: per-run spend cap passed to Claudo (0 = none)
    judge_enabled: bool = False
    judge_thinking_tokens: int | None = None  # None = model default (expensive); 0 disables thinking
    models: dict[str, str] = field(default_factory=dict)
    gates: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_GATES))
    gate_commands: dict[str, str] = field(default_factory=dict)

    def gates_for(self, maturity: str) -> list[str]:
        return list(self.gates.get(maturity, DEFAULT_GATES[maturity]))


def find_root(start: Path | None = None) -> Path:
    """AI_FACTORY_ROOT wins; otherwise walk up from `start` (cwd) to the first factory.toml."""
    env = os.environ.get("AI_FACTORY_ROOT")
    if env:
        return Path(env).resolve()
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / CONFIG_NAME).is_file():
            return d
    raise ConfigError(f"no {CONFIG_NAME} found from {here} upwards (set AI_FACTORY_ROOT)")


def load_config(root: Path) -> Config:
    path = root / CONFIG_NAME
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"missing {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e

    f = data.get("factory", {})
    agent = data.get("agent", {})
    gates = data.get("gates", {})
    cfg = Config(
        root=root,
        name=f.get("name", "AI Software Factory"),
        radar_path=root / f.get("radar", "radar.toml"),
        work_dir=root / f.get("work_dir", "work"),
        apps_dir=root / f.get("apps_dir", "apps"),
        golden_paths_dir=root / f.get("golden_paths_dir", "golden_paths"),
        runner=agent.get("runner", "offline"),
        max_turns_build=int(agent.get("max_turns_build", 40)),
        max_build_attempts=max(1, int(agent.get("max_build_attempts", 3))),
        judge_enabled=bool(agent.get("judge", False)),
        judge_thinking_tokens=agent.get("judge_thinking_tokens"),
        claudo_home=data.get("engine", {}).get("claudo") or None,
        plan_lint_retries=max(0, int(data.get("engine", {}).get("plan_lint_retries", 2))),
        delivery_provider=data.get("delivery", {}).get("provider", "none"),
        delivery_owner=data.get("delivery", {}).get("owner", ""),
        repo_prefix=data.get("delivery", {}).get("repo_prefix", "app-"),
        claudo_build_from=data.get("engine", {}).get("build_from", "mvp"),
        exception_days=max(0, int(data.get("policy", {}).get("exception_days", 180))),
        spec_lint_retries=max(0, int(data.get("policy", {}).get("spec_lint_retries", 2))),
        claudo_timeout=max(60, int(data.get("engine", {}).get("build_timeout", 3600))),
        claudo_budget_usd=max(0.0, float(data.get("engine", {}).get("budget_usd", 0))),
        models=dict(agent.get("models", {})),
        gate_commands=dict(gates.get("commands", {})),
    )
    for maturity in DEFAULT_GATES:
        if maturity in gates:
            cfg.gates[maturity] = list(gates[maturity])
    if cfg.delivery_provider not in ("none", "github"):
        raise ConfigError(
            f"{path}: [delivery] provider = {cfg.delivery_provider!r}, expected 'none' or 'github'"
        )
    if cfg.claudo_build_from not in DEFAULT_GATES:
        raise ConfigError(
            f"{path}: [engine] build_from = {cfg.claudo_build_from!r}, "
            f"expected one of {sorted(DEFAULT_GATES)}"
        )
    return cfg
