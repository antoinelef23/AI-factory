"""LLM-as-judge for factory artifacts (spec, plan, build). ADVISORY, never a blocking gate.

Why advisory: blocking gates stay deterministic and explainable (ROADMAP principle P6). The
judge exists to (a) tell the human reviewer at a checkpoint where an artifact looks weak,
and (b) feed telemetry / model arbitration. Its safeguards against judge hallucination:

  - every criterion must come with a VERBATIM quote from the artifact; code checks the quote
    really occurs there ("grounded"); ungrounded scores are flagged and do not count;
  - the verdict is computed from the scores by code, never taken from the model;
  - the judge sees the artifact and the original idea, not who wrote it, and defaults to a
    different (cheaper) model than the generator.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from dataclasses import asdict, dataclass, field
from typing import Protocol

Criterion = tuple[str, str]  # (id, what a 5/5 looks like)

RUBRICS: dict[str, list[Criterion]] = {
    "spec": [
        (
            "fidelity",
            "captures EVERY concrete requirement and number of the idea AND adds no business rule, number, "
            "limit, target or fixed list the idea does not state (score 3 or less if it invents any; pure "
            "technical elaboration of the idea's own rules, such as an error format, is fine). The factory "
            "MANDATES two additions in every spec, which are NOT inventions: a `GET /health` behavior and an "
            "invariant that only technologies allowed by the company tech radar are used",
        ),
        (
            "testability",
            "every invariant and behavior is a testable statement with a stable ID (INV/BHV/EX/EVAL)",
        ),
        ("unambiguity", "no vague words ('fast', 'simple') without a number; business terms defined once"),
        ("eval_coverage", "every BHV and INV is covered by at least one executable eval in the evals table"),
        (
            "examples",
            "a dedicated examples section with concrete input to output pairs, at least one per behavior "
            "(no examples section: score 1 or 2); edge cases only where the idea or the spec's behaviors "
            "define them: an edge case the idea never asked for is a fidelity problem, never a plus",
        ),
        ("scope", "explicit non-goals; the spec does not choose technologies (that is the design's job)"),
    ],
    "plan": [
        ("traceability", "every task cites spec IDs that exist; every BHV/INV is implemented by some task"),
        ("sequencing", "depends_on is sound, parallel tasks touch disjoint files, no cycle"),
        ("granularity", "tasks are small, each with a checkable done_when; tests come before code"),
        ("stack_adherence", "uses only technologies from the design's stack section; nothing invented"),
        ("human_checkpoint", "ends with a blocking human checkpoint; nothing merges or deploys unattended"),
    ],
    "build": [
        ("spec_compliance", "each behavior of the spec is implemented as written, including numeric rules"),
        ("test_quality", "tests assert real behavior and edge cases of the spec, not just that code runs"),
        ("stack_adherence", "only technologies from the design are used; golden-path structure preserved"),
        ("code_quality", "readable, small functions, validation at boundaries, no dead code or secrets"),
    ],
}

PASS_MIN, REVISE_MIN = 4, 3  # all scores >= 4 and avg >= 4 -> pass; any <= 2 -> fail; else revise


class JudgeRunner(Protocol):
    def run(self, prompt: str, **kw): ...


@dataclass
class CriterionScore:
    id: str
    score: int
    evidence: str
    quote: str = ""
    grounded: bool = False


@dataclass
class JudgeReport:
    kind: str
    criteria: list[CriterionScore] = field(default_factory=list)
    summary: str = ""
    verdict: str = "unreliable"  # pass | revise | fail | unreliable
    average: float = 0.0
    problems: list[str] = field(default_factory=list)  # judge-output problems (missing/ungrounded)
    cost_usd: float = 0.0
    model: str = ""
    raw: str = ""  # the judge's answer, kept (truncated) so an unreliable verdict can be audited
    votes: list[str] = field(default_factory=list)  # each vote's verdict when the judge ran as a panel

    def to_dict(self) -> dict:
        return asdict(self)

    def short(self) -> str:
        weak = [f"{c.id}={c.score}" for c in self.criteria if c.grounded and c.score < PASS_MIN]
        tail = f" weak: {', '.join(weak)}" if weak else ""
        panel = f" [votes: {', '.join(self.votes)}]" if len(self.votes) > 1 else ""
        return f"judge {self.kind}: {self.verdict} ({self.average:.1f}/5){tail}{panel}"

    def markdown(self) -> str:
        out = [f"# Judge report: {self.kind} ({self.verdict}, {self.average:.1f}/5)", ""]
        out.append(f"_Advisory only. Model: {self.model or 'n/a'}, cost ${self.cost_usd:.3f}._")
        if len(self.votes) > 1:
            out.append(
                f"_Panel of {len(self.votes)} runs ({', '.join(self.votes)}): each score is the median of "
                "the grounded scores of the reliable runs._"
            )
        out += ["", self.summary, "", "| criterion | score | grounded | evidence |", "|---|---|---|---|"]
        for c in self.criteria:
            out.append(
                f"| {c.id} | {c.score} | {'yes' if c.grounded else 'NO'} | {c.evidence.replace('|', '/')} |"
            )
        if self.problems:
            out += ["", "Judge output problems:"] + [f"- {p}" for p in self.problems]
        if self.verdict == "unreliable" and self.raw:
            out += ["", "Raw judge answer (for audit):", "", "```", self.raw, "```"]
        return "\n".join(out) + "\n"


def _norm(text: str) -> str:
    """Case, whitespace, markdown decoration and list-bullet markers are not content."""
    text = re.sub(r"[`*_#>|]", " ", text)
    text = re.sub(r"(?<!\S)[-•]+(?!\S)", " ", text)  # standalone '-' / bullets: '- a' and 'a' are the same
    return re.sub(r"\s+", " ", text).strip().lower()


MIN_FRAGMENT, MIN_GROUNDED_CHARS = 12, 24
_ELLIPSIS = re.compile(r"\.{3}|…")


def is_grounded(quote: str, haystack: str) -> bool:
    """A quote is grounded when it is made of REAL text of the artifact.

    Judges often stitch two passages with "..." (or join a bullet to its sub-bullet), so the quote is
    split on ellipses and every fragment must occur in the artifact, in order. Fabricated text fails;
    trivially short fragments are ignored but cannot carry a quote alone."""
    pos, grounded_chars = 0, 0
    for fragment in _ELLIPSIS.split(quote):
        frag = _norm(fragment)
        if len(frag) < MIN_FRAGMENT:
            continue
        found = haystack.find(frag, pos)
        if found < 0:
            return False
        pos = found + len(frag)
        grounded_chars += len(frag)
    return grounded_chars >= MIN_GROUNDED_CHARS


ABSENT_PREFIX = "absent:"


# What an ABSENT claim may name: the parts of an artifact (a made-up name would otherwise ground any low
# score simply by not occurring: audit A145, A146).
ABSENT_VOCABULARY = {
    "intent", "kpi", "target kpi", "glossary", "invariants", "behaviors", "behaviours", "examples",
    "non-goals", "evals", "tasks", "depends_on", "files_touched", "verify", "checkpoint", "tests",
}  # fmt: skip


def _section_empty(artifact: str, element: str) -> bool:
    """A `## n. <Element>` heading whose section holds nothing but blank lines."""
    found = re.search(
        rf"(?ims)^##\s+\d*\.?\s*{re.escape(element)}\b[^\n]*\n(.*?)(?=^##\s|\Z)", artifact + "\n"
    )
    return bool(found) and not found.group(1).strip()


def absence_holds(quote: str, score: int, haystack: str, artifact: str = "") -> bool:
    """`ABSENT: <element>` grounds a LOW score for something missing entirely: nothing can be quoted from a
    section that does not exist, so without this an evals table removed outright left the criterion
    unscored (calibration 2026-10-09). It holds only for a known part of an artifact, scored 1 or 2, that
    really does not occur (normalised), or whose heading is left with nothing under it: a false absence claim
    is discarded like a made-up quote."""
    text = _norm(quote)
    if not text.startswith(ABSENT_PREFIX) or score > 2:
        return False
    element = re.sub(r"\s+(table|section)$", "", text[len(ABSENT_PREFIX) :].strip())
    if element not in ABSENT_VOCABULARY:
        return False
    return element not in haystack or _section_empty(artifact, element)


def build_prompt(kind: str, artifact: str, idea: str, extra: str = "") -> str:
    rubric = "\n".join(f"- {cid}: 5 means: {desc}" for cid, desc in RUBRICS[kind])
    ids = ", ".join(cid for cid, _ in RUBRICS[kind])
    return f"""You are a strict, independent reviewer of a {kind} produced by an AI software factory.
You do not know who wrote it. Score ONLY what is on the page; never reward intent or length.

ORIGINAL BUSINESS IDEA (the source of truth for fidelity):
<idea>
{idea.strip()}
</idea>
{extra}
ARTIFACT UNDER REVIEW:
<artifact>
{artifact.strip()}
</artifact>

Score each criterion from 1 to 5 (5 = fully meets it, 3 = partially, 1 = absent or wrong):
{rubric}

For EVERY criterion give a `quote`: an EXACT excerpt (max 160 chars, copied verbatim, no
paraphrase) from the artifact that justifies the score. A quote that does not occur verbatim in the
artifact is discarded. When the element a criterion needs is MISSING ENTIRELY (for example there is no
evals table at all), write the quote as `ABSENT: <name of the missing element>` (e.g. `ABSENT: Evals`) and
score 1 or 2: it is checked that this name really does not occur in the artifact.

Answer with ONLY this JSON (no prose, no code fence):
{{"criteria": [
  {{"id": "<one of: {ids}>", "score": <1-5>, "evidence": "<one sentence>", "quote": "<verbatim>"}}],
 "summary": "<two sentences: the main weakness and the main strength>"}}
"""


def extract_json(text: str) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def compute_verdict(scores: list[int]) -> tuple[str, float]:
    if not scores:
        return "unreliable", 0.0
    avg = sum(scores) / len(scores)
    if min(scores) <= 2:
        return "fail", avg
    if min(scores) >= PASS_MIN and avg >= PASS_MIN:
        return "pass", avg
    return "revise", avg


def evaluate(kind: str, raw: str, artifact: str) -> JudgeReport:
    """Turn the judge's raw answer into a validated report. Pure and deterministic."""
    report = JudgeReport(kind=kind)
    data = extract_json(raw)
    if data is None:
        report.problems.append("judge answer is not valid JSON")
        return report
    report.summary = str(data.get("summary", "")).strip()
    wanted = {cid for cid, _ in RUBRICS[kind]}
    haystack = _norm(artifact)
    seen: set[str] = set()
    criteria = data.get("criteria")
    if not isinstance(criteria, list):
        report.problems.append("judge answer has no criteria list")
        criteria = []
    for raw_c in criteria:
        if not isinstance(raw_c, dict):
            continue
        cid, score = raw_c.get("id"), raw_c.get("score")
        if not isinstance(cid, str) or cid not in wanted or cid in seen:
            report.problems.append(f"unexpected or duplicate criterion {cid!r}")
            continue
        if not isinstance(score, int) or isinstance(score, bool) or not 1 <= score <= 5:
            report.problems.append(f"{cid}: score {score!r} is not an integer 1-5")
            continue
        seen.add(cid)
        quote = str(raw_c.get("quote", "")).strip()
        grounded = is_grounded(quote, haystack) or absence_holds(quote, score, haystack, artifact)
        if not grounded:
            report.problems.append(f"{cid}: quote not found verbatim in the artifact (score ignored)")
        report.criteria.append(
            CriterionScore(cid, score, str(raw_c.get("evidence", "")).strip(), quote, grounded)
        )
    for missing in sorted(wanted - seen):
        report.problems.append(f"{missing}: not scored by the judge")
    grounded_scores = [c.score for c in report.criteria if c.grounded]
    # Unreliable unless most criteria are both present and grounded: do not average hallucinations.
    if len(grounded_scores) * 2 <= len(wanted):
        report.verdict, report.average = "unreliable", 0.0
        if grounded_scores:
            report.average = sum(grounded_scores) / len(grounded_scores)
        return report
    report.verdict, report.average = compute_verdict(grounded_scores)
    return report


def judge(
    runner: JudgeRunner,
    kind: str,
    artifact: str,
    idea: str,
    *,
    model: str | None,
    cwd,
    extra: str = "",
    thinking_tokens: int | None = None,
) -> JudgeReport:
    """One judge call. A runner failure yields an `unreliable` report, never an exception."""
    if kind not in RUBRICS:
        raise ValueError(f"unknown artifact kind {kind!r} (expected one of {sorted(RUBRICS)})")
    # No tools, one turn: the artifact is in the prompt, so reading files would only add cost.
    result = runner.run(
        build_prompt(kind, artifact, idea, extra),
        cwd=cwd,
        model=model,
        tools=[],
        max_turns=1,
        thinking_tokens=thinking_tokens,
    )
    if not result.ok:
        report = JudgeReport(kind=kind, problems=[f"judge call failed: {result.error or 'empty'}"])
    else:
        report = evaluate(kind, result.text, artifact)
        report.raw = result.text[:6000]
    report.cost_usd, report.model = result.cost_usd, model or ""
    return report


def combine(kind: str, reports: list[JudgeReport]) -> JudgeReport:
    """One verdict from several runs of the judge on the same artifact.

    A single run is noisy (the 2026-10-08 calibrations moved by a whole criterion between runs), so: each
    criterion takes the MEDIAN of its grounded scores across the reliable runs, and the verdict is computed
    from those medians by the usual rule. If most runs are unreliable, the panel is unreliable."""
    if len(reports) == 1:
        return reports[0]
    out = JudgeReport(kind=kind, model=reports[0].model, votes=[r.verdict for r in reports])
    out.cost_usd = sum(r.cost_usd for r in reports)
    reliable = [r for r in reports if r.verdict != "unreliable"]
    for r in reports:
        out.problems += [p for p in r.problems if p not in out.problems]
    if len(reliable) * 2 <= len(reports):
        out.verdict = "unreliable"
        out.raw = next((r.raw for r in reports if r.raw), "")
        out.summary = f"{len(reports) - len(reliable)} of {len(reports)} runs were unreliable"
        return out
    for cid, _ in RUBRICS[kind]:
        scored = [(c.score, c) for r in reliable for c in r.criteria if c.id == cid and c.grounded]
        if not scored:
            continue
        # Half up: with two runs at 3 and 5 the criterion is 4, never the minimum of the two (audit A147).
        median = math.floor(statistics.median(score for score, _ in scored) + 0.5)
        chosen = min(scored, key=lambda sc: abs(sc[0] - median))[1]
        votes = ", ".join(str(score) for score, _ in scored)
        out.criteria.append(
            CriterionScore(cid, median, f"{chosen.evidence} (scores: {votes})", chosen.quote, True)
        )
    # No "too few criteria" case: every reliable run grounds more than half of them, and each one it
    # grounds is kept here, so the panel always has more than half.
    out.verdict, out.average = compute_verdict([c.score for c in out.criteria])
    agreeing = [r for r in reliable if r.verdict == out.verdict] or reliable
    out.summary = agreeing[0].summary
    return out


def judge_panel(
    runner: JudgeRunner,
    kind: str,
    artifact: str,
    idea: str,
    *,
    votes: int,
    model: str | None,
    cwd,
    extra: str = "",
    thinking_tokens: int | None = None,
) -> JudgeReport:
    """`votes` independent runs of the judge, combined (median per criterion). votes = 1 is one plain run."""
    reports = [
        judge(
            runner, kind, artifact, idea, model=model, cwd=cwd, extra=extra, thinking_tokens=thinking_tokens
        )
        for _ in range(max(1, votes))
    ]
    return combine(kind, reports)
