"""Radar -> design compiler.

The stack is NOT chosen by an LLM: it is compiled from the IT tech radar, one technology
per capability the idea needs. That is what lets IT trust the factory. An agent may add
prose later, but never another technology.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from factory.radar import ALLOW, APPROVAL, BLOCK, Radar, Tech, verdict

BASE_CAPABILITIES = ["language", "backend", "testing", "quality", "ci", "hosting"]
CAPABILITY_KEYWORDS = {
    "frontend": r"\b(ui|interface|dashboard|pages?|forms?|front[- ]?end|web app|screens?|portal)\b",
    "database": r"\b(stor(e|es|ed)|sav(e|es|ed)|persist\w*|database|history|records?|track\w*)\b",
    "ai": r"\b(ai|llm|gpt|claude|summari[sz]\w*|classif\w*|chatbot|assistant|generat\w*)\b",
    "messaging": r"\b(event|events|queue|stream|streaming|kafka|pub/?sub)\b",
}


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
