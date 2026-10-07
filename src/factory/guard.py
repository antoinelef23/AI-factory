"""radar_guard: does a project respect the company tech radar at a given maturity?

Deterministic, offline, explainable: every violation names the technology, its ring,
where it was found and what IT suggests instead. Works on ANY repo (`factory check`),
not only on apps the factory built.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from factory.detect import scan_project
from factory.radar import ALLOW, APPROVAL, BLOCK, Radar, normalize, verdict

# Design docs list forbidden technologies on purpose; those regions are not usage.
IGNORE_BLOCK = re.compile(r"<!--\s*radar:ignore\s*-->.*?<!--\s*/radar:ignore\s*-->", re.S | re.I)

# A plan line that names a technology only to forbid it ("Do not add Flask") is a guardrail, not usage.
NEGATION = re.compile(
    r"\b(not|never|no|don't|dont|avoid|without|forbidden|prohibited|instead of|rather than|replace[sd]?)\b",
    re.I,
)
TASK_HEADER = re.compile(r"^###\s+((?:T|CP-)\d+)\b")


def plan_radar_errors(tasks_md: str, radar: Radar, maturity: str) -> list[str]:
    """Technologies the radar BLOCKS at `maturity` that a plan asks the agents to USE.

    Runs before any agent executes, so a plan aimed at a forbidden stack costs nothing. Only lines
    without a negation count: plans legitimately say "Do not add Flask, MongoDB". The build-time radar
    gate (manifests, imports, images) remains the real enforcement; this one only saves the spend."""
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    task = "plan"
    for line in tasks_md.splitlines():
        header = TASK_HEADER.match(line)
        if header:
            task = header.group(1)
            continue
        if NEGATION.search(line):
            continue
        for tech in radar.scan_text(line):
            if verdict(tech, maturity) != BLOCK or (task, tech.id) in seen:
                continue
            seen.add((task, tech.id))
            alt = radar.get(tech.replaced_by) if tech.replaced_by else None
            errors.append(
                f"{task}: asks to use {tech.name} ({tech.ring}), not allowed at {maturity}"
                + (f"; use {alt.name} instead" if alt else "")
            )
    return errors


@dataclass(frozen=True)
class Violation:
    key: str  # tech id, or normalized package name when not on the radar
    name: str
    ring: str | None  # None = not on the radar
    verdict: str  # needs_it_approval | block
    sources: tuple[str, ...]
    hint: str

    def describe(self) -> str:
        ring = self.ring or "not on radar"
        where = ", ".join(self.sources[:3]) + (" ..." if len(self.sources) > 3 else "")
        return f"[{self.verdict}] {self.name} ({ring}) in {where}: {self.hint}"


@dataclass
class GuardReport:
    maturity: str
    violations: list[Violation] = field(default_factory=list)
    allowed: list[str] = field(default_factory=list)  # tech ids found and allowed

    def blocking(self, exceptions: set[str] | frozenset[str] = frozenset()) -> list[Violation]:
        """Violations that stop shipping: blocks, plus approvals IT has not granted."""
        return [
            v
            for v in self.violations
            if v.verdict == BLOCK or (v.verdict == APPROVAL and v.key not in exceptions)
        ]

    def needs_approval(self) -> list[Violation]:
        return [v for v in self.violations if v.verdict == APPROVAL]

    def ok(self, exceptions: set[str] | frozenset[str] = frozenset()) -> bool:
        return not self.blocking(exceptions)


def _hint(radar: Radar, tech_id: str | None, v: str, maturity: str) -> str:
    tech = radar.get(tech_id) if tech_id else None
    if tech is None:
        return "not on the tech radar: ask IT to assess it, or use an adopted alternative"
    if v == BLOCK:
        if tech.ring == "hold":
            alt = radar.get(tech.replaced_by) if tech.replaced_by else None
            return f"on hold at {radar.company or 'the company'}" + (
                f": use {alt.name} instead" if alt else ""
            )
        return f"'{tech.ring}' technologies are not allowed at {maturity}"
    return f"'{tech.ring}' technology at {maturity}: needs explicit IT approval"


def check_project(root: Path, radar: Radar, maturity: str, docs: list[Path] | None = None) -> GuardReport:
    """Scan code/manifests under `root` plus free-text design `docs`."""
    seen: dict[str, tuple[str, str | None, str, list[str]]] = {}  # key -> (name, tech_id, verdict, sources)

    def record(key: str, name: str, tech_id: str | None, v: str, source: str) -> None:
        entry = seen.setdefault(key, (name, tech_id, v, []))
        if source not in entry[3]:
            entry[3].append(source)

    for f in scan_project(root):
        tech = radar.find(f.name)
        if tech is None and f.kind == "import":
            continue  # stdlib / local modules: only declared deps count as unknown
        v = verdict(tech, maturity)
        key = tech.id if tech else normalize(f.name)
        record(key, tech.name if tech else f.name, tech.id if tech else None, v, f.source)

    for doc in docs or []:
        if not doc.is_file():
            continue
        text = IGNORE_BLOCK.sub("", doc.read_text(encoding="utf-8"))
        rel = doc.relative_to(root).as_posix() if doc.is_relative_to(root) else doc.name
        for tech in radar.scan_text(text):
            record(tech.id, tech.name, tech.id, verdict(tech, maturity), rel)

    report = GuardReport(maturity=maturity)
    for key, (name, tech_id, v, sources) in sorted(seen.items()):
        if v == ALLOW:
            report.allowed.append(key)
            continue
        ring = radar.get(tech_id).ring if tech_id else None
        report.violations.append(
            Violation(key, name, ring, v, tuple(sources), _hint(radar, tech_id, v, maturity))
        )
    return report
