"""Offline artifact templates and agent prompts (Claudo triplet format)."""

from __future__ import annotations

import re

from factory.workitem import WorkItem

# Same token shape as Claudo's plan model: the IDs a task may cite in `implements`.
SPEC_ID = re.compile(r"\b(?:INV|BHV|EX|EVAL|NG)-[A-Za-z0-9]+\b")


def idea_md(item: WorkItem) -> str:
    return (
        f"# Idea: {item.title}\n\n"
        f"- requester: {item.requester}\n"
        f"- maturity target: {item.maturity}\n"
        f"- submitted: {item.created}\n\n"
        f"## What the business wants\n\n{item.idea.strip()}\n"
    )


def offline_spec(item: WorkItem) -> str:
    return f"""---
type: spec
feature: {item.slug}
version: 0.1.0
status: draft
owner: {item.requester}
validated_by:
---

# Spec: {item.title}

> The WHAT. Drafted offline from the idea; the business refines it before approving.

## 1. Intent

{item.idea.strip()}

**Target KPI:** to be set by the business at spec review.

## 2. Glossary

| Business term | Canonical name (code) | Definition |
|---|---|---|
| {item.title} | `{item.slug.replace("-", "_")}` | the application described in the intent |

## 3. Invariants

- **INV-1**: The application MUST only use technologies allowed by the company tech radar at maturity
  `{item.maturity}`.
- **INV-2**: The application MUST NOT contain secrets in its source code.

## 4. Behaviors

### BHV-1: service is reachable
- **Given** the application is running
- **When** a client calls `GET /health`
- **Then** it answers HTTP 200 with `{{"status": "ok"}}`

### BHV-2: core use case
- **Given** a user of {item.title}
- **When** they perform the main action described in the intent
- **Then** the outcome described in the intent is observable

## 5. Examples

### EX-1: health check
```yaml
input:
  request: GET /health
expected_output:
  status_code: 200
  body: {{status: ok}}
covers: [BHV-1]
```

## 6. Non-goals

- **NG-1**: Anything not stated in the intent above.

## 7. Evals: merge gate

| ID | Type | Description | Covers | Success threshold |
|---|---|---|---|---|
| EVAL-1 | deterministic | `GET /health` returns 200 and status ok | BHV-1 | 100% |
| EVAL-2 | deterministic | radar gate passes at `{item.maturity}` | INV-1 | 100% |
| EVAL-3 | deterministic | secrets gate passes | INV-2 | 100% |
"""


def offline_tasks(item: WorkItem, golden_path: str | None) -> str:
    """tasks.md in the exact format Claudo's plan parser reads (`### T1 — title`, bracketed lists,
    backticked files_touched, a `trigger` on the checkpoint). Lint-clean against the real engine."""
    scaffold = f"golden_paths/{golden_path}" if golden_path else "the IT golden path"
    return f"""---
type: tasks
feature: {item.slug}
version: 0.1.0
status: proposed
generated_by: planner (offline)
spec: ./spec.md          # version: 0.1.0
design: ./design.md      # version: 0.1.0
---

# Tasks — {item.title}

### T1 — Scaffold the app from the golden path
- **depends_on :** []
- **implements :** [BHV-1, INV-1]
- **files_touched :** `app/`, `tests/`, `pyproject.toml`
- **verify :** `uv run pytest -q`
- **done_when :** the app starts from {scaffold} and `GET /health` returns 200 ok.
- **prompt :**
  > Scaffold the application from {scaffold}. Keep its structure, CI and Dockerfile.
  > Use ONLY the stack of design.md section 3. Write the `GET /health` test first (BHV-1).

### T2 — Implement the core use case
- **depends_on :** [T1]
- **implements :** [BHV-2]
- **files_touched :** `app/`, `tests/`
- **verify :** `uv run pytest -q`
- **done_when :** the behavior of the intent in spec.md section 1 is covered by passing tests.
- **prompt :**
  > Implement the main use case described in spec.md section 1 (BHV-2). Tests first.
  > No secrets in code; configuration from environment variables.

### T3 — Evals and gates
- **depends_on :** [T2]
- **implements :** [INV-1, INV-2, EVAL-1, EVAL-2, EVAL-3]
- **files_touched :** `tests/`
- **verify :** `uv run pytest -q`
- **done_when :** every eval of spec.md section 7 passes at maturity `{item.maturity}`.
- **prompt :**
  > Make every eval of spec.md section 7 executable and green. Only touch app code to fix a defect.

### CP-1 — Ship review (the merge is human)
- **trigger :** auto when [T3] done
- **mode :** blocking
"""


def spec_prompt(item: WorkItem, feedback: str) -> str:
    fb = f"\nThe business rejected the previous draft. Their feedback:\n{feedback}\n" if feedback else ""
    return f"""You are the spec writer of a governed AI software factory.
Read idea.md in the current directory. Write spec.md for it, in the format below.
Rules: every statement testable with a stable ID (INV-n, BHV-n, EX-n, EVAL-n); no vague words
without numbers; realistic examples; explicit non-goals; one eval per BHV/INV.
Always include BHV-1 = `GET /health` returns 200 {{"status": "ok"}} and
INV-1 = only technologies allowed by the company tech radar at maturity `{item.maturity}`.
Do not choose technologies: that is the design's job.{fb}
Output ONLY the markdown of spec.md (frontmatter first: type: spec, feature: {item.slug},
version: 0.1.0, status: draft), no commentary, no code fence.

Sections: 1. Intent (+ Target KPI), 2. Glossary, 3. Invariants, 4. Behaviors (Given/When/Then),
5. Examples (yaml), 6. Non-goals, 7. Evals (table: ID, Type, Description, Covers, Success threshold).
"""


PLAN_FORMAT = """EXACT FORMAT (a parser reads it; any deviation makes the plan unreadable):
- Frontmatter: type: tasks, feature, version: 0.1.0, status: proposed, spec: ./spec.md  # version: 0.1.0
- One section per task, header EXACTLY `### T1 — Short title` (T + number, space, EM DASH, space).
- Fields, one per line, each written `- **name :** value`:
    **depends_on :** [T1, T2]            bracketed list of task IDs; `[]` when none
    **implements :** [BHV-1, INV-2]      spec IDs that EXIST in spec.md; some task must carry each EVAL-n
    **files_touched :** `app/`, `tests/` each path in backticks; parallel tasks must not overlap
    **verify :** `uv run pytest -q`      ONE command in backticks (uv/pytest/ruff/just/make/python)
    **done_when :** one checkable sentence
    **prompt :** then the instructions as lines starting with two spaces and `> `
- The LAST section is a human checkpoint, written exactly like this:
    ### CP-1 — Ship review (the merge is human)
    - **trigger :** auto when [T3] done
    - **mode :** blocking
"""


def plan_prompt(item: WorkItem, feedback: str, spec_ids: list[str] | None = None) -> str:
    fb = f"\nThe owner rejected the previous plan. Their feedback:\n{feedback}\n" if feedback else ""
    ids = f"\nSpec IDs you may reference in `implements`: {', '.join(spec_ids)}.\n" if spec_ids else ""
    return f"""You are the planner of a governed AI software factory.
Read spec.md and design.md in the current directory. Write tasks.md: the execution plan.
Rules: T1 scaffolds from the golden path named in design.md section 2; small tasks; tests
before code; use ONLY the stack in design.md section 3; every BHV and INV of the spec is
implemented by some task.{ids}{fb}
{PLAN_FORMAT}
Output ONLY the markdown of tasks.md (feature: {item.slug}), no commentary, no code fence.
"""


def build_prompt(item: WorkItem, forbidden: list[str], feedback: str) -> str:
    fb = f"\nA previous attempt failed the factory gates. Fix these first:\n{feedback}\n" if feedback else ""
    return f"""You are the implementer of a governed AI software factory.
The current directory is the app `{item.slug}`, already scaffolded from the IT golden path.
Read work/{item.slug}/spec.md, design.md and tasks.md (in that order), then implement the
spec by following tasks.md.

HARD RULES (enforced by gates after you finish; violations block shipping):
- Use ONLY the technologies in design.md section 3. Never add a dependency outside it.
- Forbidden at maturity `{item.maturity}`: {", ".join(forbidden) or "none"}.
- No secrets, tokens or passwords in code; read config from environment variables.
- Write pytest tests for every BHV and INV; run `uv run pytest -q` until green.
- Keep the golden-path structure, pyproject.toml layout and .github/ untouched unless a task says so.
- Never edit files under work/ (the spec is immutable during the build).{fb}
Finish with a 5-line summary of what you built.
"""
