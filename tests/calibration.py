"""Deliberate degradations of a known-good spec, each aimed at ONE rubric criterion.

Used to calibrate the LLM judge: if it cannot tell the good spec from these, its advice at a checkpoint is
worthless. Pure text functions, so they are unit-tested for free; the judge calls are the billed part.
"""

from __future__ import annotations

import re
from collections.abc import Callable

INVENTED = (
    "- **INV-6**: The service MUST sustain 10,000 requests per second with p99 latency under 5 ms "
    "and MUST require OAuth2 login for every endpoint.\n"
)


def _cut_section(spec: str, heading: str, until: str | None) -> str:
    start = spec.index(heading)
    end = spec.index(until, start) if until else len(spec)
    return spec[:start] + spec[end:]


def no_evals(spec: str) -> str:
    """Nothing executable gates the merge: section 7 (the evals table) is gone."""
    return _cut_section(spec, "## 7. Evals", None)


def no_examples(spec: str) -> str:
    """No concrete input/output examples: section 5 is gone."""
    return _cut_section(spec, "## 5. Examples", "## 6. Non-goals")


def invented_requirement(spec: str) -> str:
    """A business rule, two numbers and an auth requirement that the original idea never stated."""
    return spec.replace("## 4. Behaviors", INVENTED + "\n## 4. Behaviors", 1)


def vague(spec: str) -> str:
    """Concrete outcomes AND eval thresholds replaced by vague words: what the spec rules forbid."""
    spec = re.sub(r"^(\| EVAL-\d+ \|.*\|)[^|]+\|[ \t]*$", r"\1 works well |", spec, flags=re.M)
    spec = re.sub(r"^  - And .*\n", "", spec, flags=re.M)
    return re.sub(
        r"^  - Then .*$", "  - Then the system responds appropriately and quickly", spec, flags=re.M
    )


# name -> (degradation, rubric criteria it should hurt)
DEGRADATIONS: dict[str, tuple[Callable[[str], str], tuple[str, ...]]] = {
    "no_evals": (no_evals, ("eval_coverage", "testability")),
    "no_examples": (no_examples, ("examples",)),
    "invented_requirement": (invented_requirement, ("fidelity", "scope")),
    "vague": (vague, ("unambiguity", "testability")),
}


def grounded_scores(report) -> dict[str, int]:
    return {c.id: c.score for c in report.criteria if c.grounded}


def compare(base, degraded, targets: tuple[str, ...]) -> tuple[str, str]:
    """Did the judge notice a degradation? -> ("caught" | "missed" | "inconclusive", why).

    Inconclusive when the comparison itself is undefined (no target criterion scored with grounded evidence in
    BOTH runs, or the degraded answer was unreliable): a measurement gap, not a verdict on the judge."""
    b, d = grounded_scores(base), grounded_scores(degraded)
    comparable = [t for t in targets if t in b and t in d]
    if degraded.verdict == "unreliable":
        return "inconclusive", "the judge's answer was unreliable (ungrounded quotes)"
    if not comparable:
        return "inconclusive", f"no grounded score for {targets} in both runs (baseline has {sorted(b)})"
    drop = max(b[t] - d[t] for t in comparable)
    detail = (
        f"max drop {drop} on {comparable}, avg {base.average:.2f} -> {degraded.average:.2f}, "
        f"{degraded.verdict}"
    )
    return ("caught" if drop >= 1 and degraded.average < base.average else "missed"), detail
