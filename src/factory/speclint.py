"""Structural lint of a spec.md: the mechanical half of reviewing a spec.

An LLM judge cannot be trusted to notice what is MISSING (the calibration showed both models miss a deleted
evals table), but a parser can: all sections present, every invariant and behavior covered by an eval, no ID
defined twice. It runs before a human sees the spec; an agent-written spec that fails is re-prompted with the
errors, exactly like a plan that fails Claudo's plan-lint.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SECTIONS = {
    "1": "Intent",
    "2": "Glossary",
    "3": "Invariants",
    "4": "Behaviors",
    "5": "Examples",
    "6": "Non-goals",
    "7": "Evals",
}
# `- **INV-1**: text`, `- INV-1: text`, `### BHV-2: title`, `**BHV-3** - text`: how real specs define an ID.
DEFINITION = re.compile(
    r"^\s*(?:[-*]\s+|#{2,4}\s+)?(?:\*\*)?(?P<id>(?:INV|BHV)-\d+)(?:\*\*)?\s*(?:[:—–-])", re.M
)
ANY_ID = re.compile(r"\b(?:INV|BHV|EX|EVAL|NG)-\d+[a-z]?\b")
TOP_LEVEL_ID = re.compile(r"\b(?:INV|BHV)-\d+\b")  # BHV-1a style sub-cases are covered through BHV-1


@dataclass
class SpecLint:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def feedback(self) -> str:
        return "\n".join([f"- {e}" for e in self.errors] + [f"- (warning) {w}" for w in self.warnings])


def _eval_rows(text: str) -> list[tuple[str, str]]:
    """(eval id, text of its 'Covers' cell) for each row of the evals table."""
    rows: list[tuple[str, str]] = []
    covers_col = None
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        lowered = [c.lower() for c in cells]
        if "covers" in lowered and "id" in lowered:
            covers_col = lowered.index("covers")
            continue
        if cells and re.fullmatch(r"EVAL-\d+", cells[0]):
            rows.append(
                (cells[0], cells[covers_col] if covers_col is not None and covers_col < len(cells) else "")
            )
    return rows


def lint_spec(text: str, *, kind: str = "app") -> SpecLint:
    """Lint a spec. `kind` is "app" (a new application) or a change kind (feature | bug | migration)."""
    result = SpecLint()
    for number, name in SECTIONS.items():
        if not re.search(rf"^##\s+{number}\.?\s", text, re.M):
            result.errors.append(f"section '## {number}. {name}' is missing")

    defined: dict[str, int] = {}
    for m in DEFINITION.finditer(text):
        defined[m["id"]] = defined.get(m["id"], 0) + 1
    for spec_id, count in sorted(defined.items()):
        if count > 1:
            result.errors.append(f"{spec_id} is defined {count} times: IDs are stable and unique")
    if not any(i.startswith("INV-") for i in defined):
        result.errors.append("no invariant (INV-n) is defined")
    if not any(i.startswith("BHV-") for i in defined):
        result.errors.append("no behavior (BHV-n) is defined")

    rows = _eval_rows(text)
    if not rows:
        result.errors.append(
            "section 7 has no eval: a table row `| EVAL-n | type | description | covers | threshold |`"
        )
    covered = {i for _eid, cell in rows for i in TOP_LEVEL_ID.findall(cell)}
    uncovered = [i for i in sorted(defined) if i not in covered]
    if rows and uncovered:
        missing = ", ".join(uncovered)
        result.errors.append(
            f"not covered by any eval ('Covers' column): {missing}; each INV and BHV needs one"
        )
    known = set(ANY_ID.findall(text))
    for eval_id, cell in rows:
        if not cell.strip():
            result.warnings.append(f"{eval_id} covers nothing: fill its 'Covers' column")
        for ref in ANY_ID.findall(cell):
            if ref not in known or (ref.startswith(("INV-", "BHV-")) and ref not in defined):
                result.warnings.append(f"{eval_id} covers {ref}, which is not defined in this spec")

    # What the factory itself mandates in every spec.
    if not re.search(r"tech(nology)?\s+radar", text, re.I):
        result.errors.append("no invariant ties the app to the company tech radar (INV-1 is mandated)")
    if kind == "app" and "/health" not in text:
        result.errors.append("`GET /health` (BHV-1) is mandated in every new app spec and is missing")
    if kind != "app" and not re.search(r"regression|existing (test|behavio)", text, re.I):
        result.errors.append(
            "a change spec must state no regression (BHV-1: the existing test suite still passes)"
        )
    return result
