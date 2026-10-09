"""Radar -> design compiler.

The stack is NOT chosen by an LLM: it is compiled from the IT tech radar, one technology
per capability the idea needs. That is what lets IT trust the factory. An agent may add
prose later, but never another technology.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from factory.radar import ALLOW, APPROVAL, BLOCK, Radar, Tech, verdict

BASE_CAPABILITIES = ["language", "backend", "testing", "quality", "ci", "hosting"]
CAPABILITY_KEYWORDS = {
    "frontend": r"\b(ui|interface|dashboard|pages?|forms?|front[- ]?end|web app|screens?|portal)\b",
    "database": r"\b(stor(e|es|ed)|sav(e|es|ed)|persist\w*|database|history|records?|track\w*)\b",
    "ai": r"\b(ai|llm|gpt|claude|summari[sz]\w*|classif\w*|chatbot|assistant|generat\w*)\b",
    "messaging": r"\b(event|events|queue|stream|streaming|kafka|pub/?sub)\b",
}


OPTIONAL_CAPABILITIES = {
    "frontend": "screens or pages people use in a browser",
    "database": "data kept between requests (records, lists, history, filters over past entries)",
    "ai": "generating, summarising or classifying text or content with a model",
    "messaging": "events, queues or streams between systems",
}


def capabilities_prompt(idea: str, offered: list[str]) -> str:
    """Ask a model which OPTIONAL capabilities an idea needs, each justified by words of the idea."""
    menu = "\n".join(f"- {c}: {OPTIONAL_CAPABILITIES[c]}" for c in offered)
    return f"""You are the capability analyst of an AI software factory. Read the business idea and say which
of these capabilities the application NEEDS. Do not choose technologies.

{menu}

<idea>
{idea.strip()}
</idea>

For each capability you select, quote the EXACT words of the idea (3 to 120 characters, copied verbatim) that
show the need. A capability without a verbatim quote is discarded. Select nothing the idea does not imply.
Answer with ONLY this JSON: {{"capabilities": [{{"id": "<capability>", "quote": "<verbatim words>"}}]}}
"""


def grounded_capabilities(answer: str, idea: str, offered: list[str]) -> tuple[list[str], list[str]]:
    """(capabilities accepted, problems) from a model answer: known capabilities only, each with a quote that
    really occurs in the idea (case and spacing ignored)."""
    import json

    start, end = answer.find("{"), answer.rfind("}")
    try:
        data = json.loads(answer[start : end + 1]) if 0 <= start < end else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("capabilities"), list):
        return [], ["the capability analyst's answer is not the expected JSON"]
    norm = lambda t: re.sub(r"\s+", " ", t).strip().lower()  # noqa: E731
    haystack, accepted, problems = norm(idea), [], []
    for entry in data["capabilities"]:
        if not isinstance(entry, dict):
            continue
        cap, quote = str(entry.get("id", "")).strip().lower(), norm(str(entry.get("quote", "")))
        if cap not in offered:
            problems.append(f"unknown capability {cap!r} ignored")
        elif len(quote) < 3 or quote not in haystack:
            problems.append(f"{cap}: quote not found in the idea, ignored")
        elif cap not in accepted:
            accepted.append(cap)
    return accepted, problems


def detect_capabilities(idea: str) -> list[str]:
    caps = list(BASE_CAPABILITIES)
    for cap, pattern in CAPABILITY_KEYWORDS.items():
        if re.search(pattern, idea, re.I):
            caps.append(cap)
    return caps


@dataclass
class DesignChoice:
    stack: dict[str, Tech] = field(default_factory=dict)  # capability -> tech
    needs_approval: list[Tech] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)  # capabilities the radar cannot fill
    replaced: list[tuple[Tech, Tech | None]] = field(
        default_factory=list
    )  # (asked-for hold tech, alternative)


def choose_stack(radar: Radar, capabilities: list[str], maturity: str, mentioned: list[Tech]) -> DesignChoice:
    choice = DesignChoice()
    mentioned_ids = {t.id for t in mentioned}
    for t in mentioned:
        if verdict(t, maturity) == BLOCK:
            choice.replaced.append((t, radar.get(t.replaced_by) if t.replaced_by else None))
    for cap in capabilities:
        candidates = [t for t in radar.by_category(cap) if verdict(t, maturity) != BLOCK]
        asked = [t for t in candidates if t.id in mentioned_ids]
        allowed = [t for t in candidates if verdict(t, maturity) == ALLOW]
        # Business asked for an allowed tech -> honour it; else IT's preferred adopted one.
        pick = (asked or sorted(allowed, key=lambda t: (not t.preferred, t.ring != "adopt")) or [None])[0]
        if pick is None:
            choice.gaps.append(cap)
            continue
        choice.stack[cap] = pick
        if verdict(pick, maturity) == APPROVAL:
            choice.needs_approval.append(pick)
    return choice


def render_design(
    *,
    slug: str,
    title: str,
    maturity: str,
    radar: Radar,
    choice: DesignChoice,
    gates: list[str],
    spec_version: str,
    golden_deps: list[str] | None = None,
) -> str:
    lines = [
        "---",
        "type: design",
        f"feature: {slug}",
        "version: 0.1.0",
        "status: draft",
        "generated_by: radar-compiler",
        f"maturity: {maturity}",
        f"radar: {radar.company} {radar.version}".rstrip(),
        f"spec: ./spec.md          # version: {spec_version}",
        "---",
        "",
        f"# Design: {title}",
        "",
        "> The HOW. The stack below is compiled from the company tech radar, not chosen by an agent.",
        "> Agents MUST NOT introduce any technology absent from section 3.",
        "",
        "## 1. Overview",
        "",
        f"Maturity target: **{maturity}**. Capabilities needed: {', '.join(choice.stack) or '-'}.",
        "",
        "## 2. Reference repositories: golden paths",
        "",
        "| Capability | Golden path | Pattern borrowed |",
        "|---|---|---|",
    ]
    paths = [(cap, t) for cap, t in choice.stack.items() if t.golden_path]
    if paths:
        lines += [f"| {cap} | `golden_paths/{t.golden_path}` | IT project template |" for cap, t in paths]
    else:
        lines.append("| - | none on the radar | ask IT for a golden path |")
    lines += [
        "",
        "## 3. Stack",
        "",
        "| Capability | Choice | Ring | Justified by |",
        "|---|---|---|---|",
    ]
    for cap, t in choice.stack.items():
        status = "IT approval required" if t in choice.needs_approval else "tech radar"
        lines.append(f"| {cap} | {t.name} | {t.ring} | {status} |")
    if golden_deps:
        lines += ["", "### Golden path dependencies (pre-approved by IT, in addition to the stack above)", ""]
        lines += ["| Dependency | Radar |", "|---|---|"]
        for dep in golden_deps:
            tech = radar.find(dep)
            label = f"{tech.name} ({tech.ring})" if tech else "pre-approved by the golden path"
            lines.append(f"| {dep} | {label} |")
        lines += ["", "Any check on dependencies (evals, lint) MUST allow these: the golden path ships them."]
    lines += ["", "## 4. Tech radar constraints", ""]
    if choice.needs_approval:
        lines.append("Needs explicit IT approval at this maturity:")
        lines += [f"- {t.name} ({t.ring})" for t in choice.needs_approval]
        lines.append("")
    if choice.gaps:
        lines.append(
            "Capabilities with no allowed technology on the radar (ask IT): " + ", ".join(choice.gaps) + "."
        )
        lines.append("")
    forbidden = [t for t in radar.techs if verdict(t, maturity) == BLOCK]
    lines.append("<!-- radar:ignore -->")
    if choice.replaced:
        lines.append("Requested by the business but not allowed (replaced):")
        for asked, alt in choice.replaced:
            lines.append(f"- {asked.name} ({asked.ring}) -> {alt.name if alt else 'no alternative: ask IT'}")
        lines.append("")
    lines.append(f"Forbidden at {maturity}: " + (", ".join(t.name for t in forbidden) or "none") + ".")
    lines.append("<!-- /radar:ignore -->")
    lines += [
        "",
        "## 5. Gates at this maturity",
        "",
        ", ".join(gates) + ". No green gate, no ship. The final merge is always human (IT).",
        "",
        "## 6. ADRs",
        "",
    ]
    if choice.needs_approval:
        for i, t in enumerate(choice.needs_approval, start=1):
            lines.append(f"- **ADR-{i}**: use {t.name} ({t.ring} ring) at {maturity}; pending IT approval.")
    else:
        lines.append("- **ADR-1**: stack fully within the adopted ring; no exception requested.")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ changes to an existing app

CHANGE_KINDS = ("feature", "bug", "migration")


@dataclass
class ExistingStack:
    """What an existing app really uses, read from its manifests, images and imports."""

    techs: list[Tech] = field(default_factory=list)  # on the radar, in first-seen order
    unknown: list[str] = field(default_factory=list)  # declared dependencies the radar does not know
    declared: list[str] = field(default_factory=list)  # every dependency name its manifests declare


def existing_stack(app: Path, radar: Radar) -> ExistingStack:
    from factory.detect import scan_project

    stack = ExistingStack()
    seen: set[str] = set()
    for f in scan_project(app):
        tech = radar.find(f.name)
        if f.kind == "manifest" and f.name not in stack.declared:
            stack.declared.append(f.name)
        if tech is not None:
            if tech.id not in seen:
                seen.add(tech.id)
                stack.techs.append(tech)
        elif f.kind == "manifest" and f.name not in stack.unknown:
            stack.unknown.append(f.name)
    return stack


def render_change_design(
    *,
    slug: str,
    title: str,
    kind: str,
    target: str,
    maturity: str,
    radar: Radar,
    existing: ExistingStack,
    gates: list[str],
    spec_version: str,
) -> str:
    """design.md of a change: the app's EXISTING stack judged by the CURRENT radar.

    Nothing is chosen: the stack already exists. The design says what to keep, what needs IT's approval and
    what to migrate away from, and forbids adding anything the radar does not already allow."""
    keep, approval, blocked = [], [], []
    for t in existing.techs:
        {ALLOW: keep, APPROVAL: approval, BLOCK: blocked}[verdict(t, maturity)].append(t)
    lines = [
        "---",
        "type: design",
        f"feature: {slug}",
        "version: 0.1.0",
        "status: draft",
        "generated_by: radar-compiler",
        f"change: {kind} of {target}",
        f"maturity: {maturity}",
        f"radar: {radar.company} {radar.version}".rstrip(),
        f"spec: ./spec.md          # version: {spec_version}",
        "---",
        "",
        # The title of a migration names what it removes ("Drop pydantic"): that is not a use of it.
        "<!-- radar:ignore -->",
        f"# Design: {title}",
        "<!-- /radar:ignore -->",
        "",
        f"> A {kind} to the EXISTING app `{target}`. Nothing is chosen: the stack below is what the app",
        "> already uses, judged by the current tech radar. Agents MUST NOT add a technology not listed here.",
        "",
        "## 1. Overview",
        "",
        f"Change kind: **{kind}**. The change runs on branch `factory/{slug}`; merging is IT's act.",
        "",
        "## 2. Existing stack (kept)",
        "",
        "| Technology | Ring | Verdict at this maturity |",
        "|---|---|---|",
    ]
    lines += [f"| {t.name} | {t.ring} | allowed |" for t in keep] or ["| - | - | nothing on the radar |"]
    if approval:
        lines += ["", "## 3. Needs IT approval at this maturity", ""]
        lines += [f"- {t.name} ({t.ring})" for t in approval]
    else:
        lines += ["", "## 3. Needs IT approval at this maturity", "", "Nothing."]
    # A dependency the radar now blocks is being migrated away from: it is NOT pre-approved (and naming it
    # here would make the design fail its own radar check); it is listed under "To migrate away from".
    existing_deps = [
        d for d in existing.declared if (t := radar.find(d)) is None or verdict(t, maturity) != BLOCK
    ]
    lines += ["", "### Existing dependencies (pre-approved: the app shipped with them)", ""]
    lines += (
        ["| Dependency |", "|---|", *(f"| {d} |" for d in existing_deps)]
        if existing_deps
        else ["None declared."]
    )
    lines += ["", "Any check on dependencies (evals, lint) MUST allow these."]
    lines += ["", "## 4. Tech radar constraints", ""]
    if existing.unknown:
        lines += [f"Declared but unknown to the radar (ask IT to assess): {', '.join(existing.unknown)}.", ""]
    lines.append("<!-- radar:ignore -->")
    if blocked:
        lines.append("To migrate away from (the radar no longer allows them at this maturity):")
        for t in blocked:
            alt = radar.get(t.replaced_by) if t.replaced_by else None
            lines.append(f"- {t.name} ({t.ring}) -> {alt.name if alt else 'no alternative: ask IT'}")
    else:
        lines.append("Nothing to migrate away from.")
    forbidden = [t for t in radar.techs if verdict(t, maturity) == BLOCK]
    lines.append(f"Forbidden at {maturity}: " + (", ".join(t.name for t in forbidden) or "none") + ".")
    lines.append("<!-- /radar:ignore -->")
    lines += [
        "",
        "## 5. Rules of the change",
        "",
        "- Existing behavior MUST NOT change except as the spec says; the existing test suite stays green.",
        "- Keep the diff minimal: touch only what the change needs.",
        "- Add no dependency; use only the technologies of section 2.",
    ]
    if blocked:
        lines.append("- Remove every technology listed under 'To migrate away from' and use its alternative.")
    lines += [
        "",
        "## 6. Gates at this maturity",
        "",
        ", ".join(gates) + ". No green gate, no ship. IT merges.",
    ]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ golden path manifests (ROADMAP P3-8)


@dataclass(frozen=True)
class GoldenPath:
    name: str
    description: str
    capabilities: tuple[str, ...]
    techs: tuple[str, ...]
    priority: int = 0
    gate_commands: dict = field(default_factory=dict)


def load_golden_paths(root: Path) -> list[GoldenPath]:
    """Every golden path with a `golden.toml` manifest under `root` (IT's templates)."""
    import tomllib

    found = []
    for manifest in sorted(root.glob("*/golden.toml")):
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        found.append(
            GoldenPath(
                name=str(data.get("name") or manifest.parent.name),
                description=str(data.get("description", "")),
                capabilities=tuple(data.get("capabilities", [])),
                techs=tuple(data.get("techs", [])),
                priority=int(data.get("priority", 0)),
                gate_commands=dict(data.get("gates", {}).get("commands", {})),
            )
        )
    return found


def pick_golden_path(
    paths: list[GoldenPath], radar: Radar, capabilities: list[str], maturity: str
) -> GoldenPath | None:
    """The template that gives structure to the most of the idea's OPTIONAL needs (frontend, messaging...),
    among those whose every technology the radar allows at `maturity` (an approval is fine, a block is not)
    and that bring no optional capability the idea does not need. Ties: higher priority, then the name."""
    needed_optional = set(capabilities) & set(OPTIONAL_CAPABILITIES)
    eligible = []
    for gp in paths:
        techs = [radar.get(t) for t in gp.techs]
        if any(t is None or verdict(t, maturity) == BLOCK for t in techs):
            continue
        extra = set(gp.capabilities) & set(OPTIONAL_CAPABILITIES) - needed_optional
        if extra:
            continue  # e.g. no React frontend for an API-only idea
        eligible.append(gp)
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda gp: (len(needed_optional & set(gp.capabilities)), gp.priority, gp.name),
    )
