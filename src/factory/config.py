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
    "mvp": ["radar", "secrets", "immutable", "tests", "dependencies", "lint", "trajectory", "clean_tree"],
    "prod": ["radar", "secrets", "immutable", "tests", "dependencies", "lint", "trajectory", "clean_tree"],
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
    identity_provider: str = "none"  # [identity] provider: none (self-declared roles) | github
    roles_path: Path = Path("roles.toml")  # [identity] roles: who holds which role (IT-owned)
    four_eyes: bool = False  # [identity] four_eyes: one person cannot decide two roles of one item
    intake_repo: str = ""  # [intake] repo: owner/name whose labelled issues are business ideas
    intake_label: str = "factory"  # [intake] label
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
    sandbox_image: str = "lab-agent:latest"  # [sandbox] image: Claudo's deploy/sandbox/Dockerfile.agent
    sandbox_network: str = "lab-egress"  # [sandbox] network: internal; the proxy is the only way out
    sandbox_proxy: str = "http://lab-egress-proxy:8888"  # [sandbox] proxy: the egress allowlist proxy
    judge_enabled: bool = False
    judge_from: str = ""  # [agent] judge_from: judge automatically from this maturity up ("" = off)
    capability_analyst: bool = True  # [agent] capability_analyst: LLM reads capabilities (P3-9)
    judge_votes: int = 1  # [agent] judge_votes: independent judge runs per artifact, median per criterion
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
            _refuse_an_apps_config(d)
            return d
    raise ConfigError(f"no {CONFIG_NAME} found from {here} upwards (set AI_FACTORY_ROOT)")


def _refuse_an_apps_config(found: Path) -> None:
    """A factory.toml inside an outer factory's apps folder belongs to an app (an agent can write it): running
    the CLI from inside an app must not make that file the factory's configuration (audit A90)."""
    for outer in found.parents:
        if not (outer / CONFIG_NAME).is_file():
            continue
        try:
            data = tomllib.loads((outer / CONFIG_NAME).read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            continue
        apps = (outer / data.get("factory", {}).get("apps_dir", "apps")).resolve()
        if found.is_relative_to(apps):
            raise ConfigError(
                f"{found / CONFIG_NAME} is inside the apps folder of the factory at {outer}: it belongs to "
                "an app, not to the factory. Run the CLI from the factory root, or set AI_FACTORY_ROOT"
            )


BUILTIN_GATES = {"radar", "secrets", "dependencies", "immutable", "trajectory", "clean_tree"}


class _Reader:
    """Typed access to factory.toml: a wrong type is a ConfigError naming the key, never a traceback nor a
    silent misreading (`judge = "false"` is truthy; a string gate list is a list of characters: A92)."""

    def __init__(self, data: dict, path: Path) -> None:
        self.data, self.path = data, path

    def table(self, name: str) -> dict:
        value = self.data.get(name, {})
        if not isinstance(value, dict):
            raise ConfigError(f"{self.path}: [{name}] must be a table")
        return value

    def get(self, section: str, key: str, kind: type, default):
        value = self.table(section).get(key, default)
        valid = (
            isinstance(value, bool)
            if kind is bool
            else isinstance(value, (int, float)) and not isinstance(value, bool)
            if kind is float
            else isinstance(value, kind) and not isinstance(value, bool)
        )
        if value is None and default is None:
            return None
        if not valid:
            raise ConfigError(f"{self.path}: [{section}] {key} = {value!r} must be a {kind.__name__}")
        return value

    def strings(self, section: str, key: str) -> dict[str, str]:
        value = self.table(section).get(key, {})
        if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
            raise ConfigError(f"{self.path}: [{section}.{key}] must map names to strings")
        return dict(value)


def load_config(root: Path) -> Config:
    path = root / CONFIG_NAME
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"missing {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e

    r = _Reader(data, path)
    gates = r.table("gates")
    thinking = r.get("agent", "judge_thinking_tokens", int, None)
    cfg = Config(
        root=root,
        name=r.get("factory", "name", str, "AI Software Factory"),
        radar_path=root / r.get("factory", "radar", str, "radar.toml"),
        work_dir=root / r.get("factory", "work_dir", str, "work"),
        apps_dir=root / r.get("factory", "apps_dir", str, "apps"),
        golden_paths_dir=root / r.get("factory", "golden_paths_dir", str, "golden_paths"),
        runner=r.get("agent", "runner", str, "offline"),
        max_turns_build=max(1, r.get("agent", "max_turns_build", int, 40)),
        max_build_attempts=max(1, r.get("agent", "max_build_attempts", int, 3)),
        judge_enabled=r.get("agent", "judge", bool, False),
        judge_from=r.get("agent", "judge_from", str, ""),
        capability_analyst=r.get("agent", "capability_analyst", bool, True),
        judge_votes=max(1, r.get("agent", "judge_votes", int, 1)),
        judge_thinking_tokens=None if thinking is None else max(0, thinking),
        claudo_home=r.get("engine", "claudo", str, "") or None,
        plan_lint_retries=max(0, r.get("engine", "plan_lint_retries", int, 2)),
        identity_provider=r.get("identity", "provider", str, "none"),
        roles_path=root / r.get("identity", "roles", str, "roles.toml"),
        four_eyes=r.get("identity", "four_eyes", bool, False),
        intake_repo=r.get("intake", "repo", str, ""),
        intake_label=r.get("intake", "label", str, "factory"),
        delivery_provider=r.get("delivery", "provider", str, "none"),
        delivery_owner=r.get("delivery", "owner", str, ""),
        repo_prefix=r.get("delivery", "repo_prefix", str, "app-"),
        claudo_build_from=r.get("engine", "build_from", str, "mvp"),
        exception_days=max(0, r.get("policy", "exception_days", int, 180)),
        spec_lint_retries=max(0, r.get("policy", "spec_lint_retries", int, 2)),
        claudo_timeout=max(60, r.get("engine", "build_timeout", int, 3600)),
        claudo_budget_usd=max(0.0, float(r.get("engine", "budget_usd", float, 0))),
        sandbox_image=r.get("sandbox", "image", str, "lab-agent:latest"),
        sandbox_network=r.get("sandbox", "network", str, "lab-egress"),
        sandbox_proxy=r.get("sandbox", "proxy", str, "http://lab-egress-proxy:8888"),
        models=r.strings("agent", "models"),
        gate_commands=r.strings("gates", "commands"),
    )
    for maturity in DEFAULT_GATES:
        if maturity in gates:
            names = gates[maturity]
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                raise ConfigError(f"{path}: [gates] {maturity} must be a list of gate names")
            unknown = [n for n in names if n not in BUILTIN_GATES and n not in cfg.gate_commands]
            if unknown:
                raise ConfigError(
                    f"{path}: [gates] {maturity}: {unknown} are neither built-in nor in [gates.commands]"
                )
            cfg.gates[maturity] = list(names)
    if cfg.four_eyes and cfg.identity_provider == "none":  # four eyes need verified identities (A116, A118)
        raise ConfigError(
            f'{path}: [identity] four_eyes needs provider = "github": self-declared names prove nothing'
        )
    if cfg.judge_from and cfg.judge_from not in DEFAULT_GATES:
        raise ConfigError(
            f"{path}: [agent] judge_from = {cfg.judge_from!r}, expected '' or one of {sorted(DEFAULT_GATES)}"
        )
    if cfg.identity_provider not in ("none", "github"):
        raise ConfigError(
            f"{path}: [identity] provider = {cfg.identity_provider!r}, expected 'none' or 'github'"
        )
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
