"""Bridge to the Claudo engine (the orchestrator + plan-lint of AI-Workflow-gates).

The factory does NOT re-implement plan validation: it runs Claudo's own `orchestrate.py --validate`
on a throwaway project holding the triplet, so a plan the factory approves is, by construction, a
plan Claudo will execute. Claudo is located, never vendored (ROADMAP D2): `[engine] claudo` in
factory.toml, then $CLAUDO_HOME, then a sibling checkout.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ENGINE_ENTRY = Path("lab") / "engine" / "orchestrate.py"
SIBLING_GUESSES = ("AI-Workflow-gates/_build", "Claudo")  # relative to the factory root's parent


class EngineError(Exception):
    pass


@dataclass
class LintResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors

    def feedback(self) -> str:
        """What to tell an agent so it can fix its plan."""
        lines = [f"- {e}" for e in self.errors] + [f"- (warning) {w}" for w in self.warnings]
        return "\n".join(lines)


def is_claudo(home: Path) -> bool:
    return (home / ENGINE_ENTRY).is_file()


def discover(configured: str | None, factory_root: Path) -> Path | None:
    """Locate a Claudo checkout. Explicit config wins; an explicit but invalid path is an error."""
    candidates: list[Path] = []
    if configured:
        explicit = Path(configured)
        explicit = explicit if explicit.is_absolute() else factory_root / explicit
        if not is_claudo(explicit):
            raise EngineError(f"[engine] claudo = {configured!r}: no {ENGINE_ENTRY} under {explicit}")
        return explicit.resolve()
    if os.environ.get("CLAUDO_HOME"):
        candidates.append(Path(os.environ["CLAUDO_HOME"]))
    candidates += [factory_root.parent / guess for guess in SIBLING_GUESSES]
    return next((c.resolve() for c in candidates if is_claudo(c)), None)


def parse_lint_output(output: str, returncode: int) -> LintResult:
    """Claudo prints one `❌ ...` line per error and one `⚠️ ...` line per warning."""
    result = LintResult(raw=output)
    for line in output.splitlines():
        text = line.strip()
        if text.startswith("❌"):
            result.errors.append(text.removeprefix("❌").strip())
        elif text.startswith("⚠"):
            result.warnings.append(text.lstrip("⚠️ ").strip())
    if returncode != 0 and not result.errors:  # crashed or refused without a parsable message
        result.errors.append(f"plan-lint exited {returncode}: {output.strip()[-400:] or 'no output'}")
    return result


class ClaudoEngine:
    def __init__(self, home: Path, python: str | None = None) -> None:
        if not is_claudo(home):
            raise EngineError(f"not a Claudo checkout: {home}")
        self.home = home
        self.python = python or sys.executable

    def lint_plan(self, slug: str, *, spec: str, design: str, tasks: str, timeout: int = 60) -> LintResult:
        """Plan-lint the triplet with Claudo's real validator (acyclic DAG, spec IDs exist, disjoint
        parallel paths, allowlisted verify commands, checkpoints...)."""
        with tempfile.TemporaryDirectory(prefix="factory-lint-", ignore_cleanup_errors=True) as tmp:
            project = Path(tmp)
            feat = project / "work" / slug
            feat.mkdir(parents=True)
            for name, text in (("spec.md", spec), ("design.md", design), ("tasks.md", tasks)):
                (feat / name).write_text(text, encoding="utf-8", newline="\n")
            (project / "pyproject.toml").write_text(
                "[project]\nname = 'lint'\nversion = '0'\n", encoding="utf-8"
            )
            argv = [self.python, str(self.home / ENGINE_ENTRY), f"work/{slug}", "--project", str(project)]
            try:
                p = subprocess.run(
                    [*argv, "--validate"],
                    cwd=self.home,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    env={**os.environ, "LAB_NO_NOTIFY": "1", "PYTHONUTF8": "1"},
                )
            except subprocess.TimeoutExpired as e:
                raise EngineError(f"plan-lint timed out after {timeout}s") from e
        return parse_lint_output(p.stdout + p.stderr, p.returncode)
