"""Radar drift: the radar is a living control, not a PDF.

IT changes radar.toml (a technology moves to hold, a new one is adopted). Two questions follow, answered
here without any model call:
  - radar_diff: what changed between two radars, and what does it newly forbid at each maturity?
  - scan_drift: which SHIPPED apps no longer comply with the CURRENT radar, and why?
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from factory.guard import Violation, check_project
from factory.radar import ALLOW, APPROVAL, BLOCK, MATURITIES, Radar, Tech, verdict
from factory.workitem import WorkItem

SEVERITY = {ALLOW: 0, APPROVAL: 1, BLOCK: 2}


@dataclass(frozen=True)
class Impact:
    """A technology became stricter at one maturity."""

    tech: Tech
    maturity: str
    before: str
    after: str


@dataclass
class RadarDiff:
    added: list[Tech] = field(default_factory=list)
    removed: list[Tech] = field(default_factory=list)
    ring_changes: list[tuple[Tech, str, str]] = field(default_factory=list)  # (new tech, old ring, new ring)
    other_changes: list[str] = field(default_factory=list)  # replaced_by, category
    impacts: list[Impact] = field(default_factory=list)  # newly stricter, per maturity

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.ring_changes or self.other_changes)


def radar_diff(old: Radar, new: Radar) -> RadarDiff:
    diff = RadarDiff()
    old_by_id = {t.id: t for t in old.techs}
    new_by_id = {t.id: t for t in new.techs}
    diff.added = [t for t in new.techs if t.id not in old_by_id]
    diff.removed = [t for t in old.techs if t.id not in new_by_id]
    for tech in new.techs:
        before = old_by_id.get(tech.id)
        if before is None:
            continue
        if before.ring != tech.ring:
            diff.ring_changes.append((tech, before.ring, tech.ring))
        if before.replaced_by != tech.replaced_by:
            diff.other_changes.append(
                f"{tech.id}: replaced_by {before.replaced_by or '-'} -> {tech.replaced_by or '-'}"
            )
        if before.category != tech.category:
            diff.other_changes.append(f"{tech.id}: category {before.category} -> {tech.category}")
        for maturity in MATURITIES:
            was, now = verdict(before, maturity), verdict(tech, maturity)
            if SEVERITY[now] > SEVERITY[was]:
                diff.impacts.append(Impact(tech, maturity, was, now))
    # A removed technology becomes "not on the radar": stricter in prod (block) and at the others (approval).
    for tech in diff.removed:
        for maturity in MATURITIES:
            was, now = verdict(tech, maturity), verdict(None, maturity)
            if SEVERITY[now] > SEVERITY[was]:
                diff.impacts.append(Impact(tech, maturity, was, now))
    return diff


def render_diff(diff: RadarDiff, old: Radar, new: Radar) -> str:
    if diff.empty:
        return f"No change between radar {old.version or '?'} and {new.version or '?'}."
    out = [f"Radar {old.version or '?'} -> {new.version or '?'}"]
    for t in diff.added:
        out.append(f"  + {t.id:<14} added in {t.ring}")
    for t in diff.removed:
        out.append(f"  - {t.id:<14} removed (was {t.ring}): apps still using it are now 'not on radar'")
    for t, before, after in diff.ring_changes:
        out.append(
            f"  ~ {t.id:<14} {before} -> {after}"
            + (f"  (use {t.replaced_by})" if after == "hold" and t.replaced_by else "")
        )
    out += [f"  ~ {c}" for c in diff.other_changes]
    if diff.impacts:
        out.append("Newly stricter:")
        for i in diff.impacts:
            out.append(f"  {i.tech.id:<14} at {i.maturity:<4} {i.before} -> {i.after}")
    return "\n".join(out)


@dataclass
class Drift:
    item: WorkItem
    violations: list[Violation]


def scan_drift(
    items: list[WorkItem],
    radar: Radar,
    apps_dir: Path,
    today: date | None = None,
    in_flight: set[str] | frozenset[str] = frozenset(),
) -> tuple[list[Drift], list[WorkItem]]:
    """(drifted apps, compliant apps) among shipped items. Honors the IT exceptions recorded for each item
    while they are in force; an EXPIRED exception no longer shelters its technology, so the app drifts."""
    today = today or date.today()
    drifted: list[Drift] = []
    clean: list[WorkItem] = []
    for item in items:
        if item.status != "shipped" or item.kind != "app":
            continue
        if item.slug in in_flight:
            continue  # the folder shows the change branch, not the delivered state: not judged until merged
        app = apps_dir / item.slug
        if not app.is_dir():
            continue
        # What the app USES (manifests, imports, images), never its old design prose: design.md records what
        # was approved at the time, so after a migration it would still name the removed technology.
        report = check_project(app, radar, item.maturity)
        blocking = report.blocking(item.active_exceptions(today))
        (drifted.append(Drift(item, blocking)) if blocking else clean.append(item))
    return drifted, clean


def migration_idea(drift: Drift, radar: Radar) -> tuple[str, str]:
    """(title, idea) for the tracking item that asks the factory to bring an app back into compliance."""
    lines = []
    for v in drift.violations:
        tech = radar.get(v.key)
        alt = radar.get(tech.replaced_by) if tech and tech.replaced_by else None
        lines.append(
            f"- {v.name} ({v.ring or 'not on radar'}): {v.hint}"
            + (f" -> migrate to {alt.name}" if alt else "")
        )
    title = f"Migrate {drift.item.slug} to the current radar"
    idea = (
        f"The app '{drift.item.slug}' ({drift.item.maturity}) no longer complies with the tech radar:\n"
        + "\n".join(lines)
        + "\nBring it back into compliance without changing its behavior."
    )
    return title, idea
