# PROGRESS

Plan: [ROADMAP.md](ROADMAP.md). Strategy: [PLANS.md](PLANS.md). How to run: [README.md](README.md).

## State (2026-10-07)

**Phase 0 complete.** MVP v0.1 plus: automatic build retries, `factory demo`, advisory LLM judge, CI,
Apache-2.0 license. Local git history only, **nothing pushed**.

**Verification command:** `just check` (ruff check + ruff format --check + pytest).
Last run: all checks passed, **87 tests passed**.

### Phase 0 work items

| ID | Status | Evidence |
|---|---|---|
| P0-1 live run | done (1 item, see below) | live1 expense-tracker: $0.71, 79 tests green first try |
| P0-2 friction fixes | done | cp1252 console crash on non-ASCII output; judge cost/leniency (below) |
| P0-3 build retries | done | `max_build_attempts`, gate output fed back; 3 tests incl. exhaustion and offline no-retry |
| P0-4 `factory demo` | done | self-checking, 0.5 s, fails if the radar is weakened (test) |
| P0-5 license + first commit | done | Apache-2.0 (D1 default taken), commits `66ab041`... |
| P0-6 CI | written, **not run** | `.github/workflows/ci.yml` (ubuntu + windows); needs a push to verify |

### Live run data (P0-1), one POC item, Sonnet 5.5 generators
Idea: expense API with a "receipt above 500 EUR" rule. Scratch copy, not committed.
- Spec 35 s / $0.11. Plan 49 s. Build 2 min 5 s / $0.45. Whole item **$0.71**, ~3.5 min of agent time.
- Gates passed on the **first attempt** (radar, secrets, 79 pytest). App: 635 lines, 7 modules, 9 test files.
- Spec quality was high (12 BHV, 8 INV, boundary examples 500.00 / 500.01) but it **invented** requirements:
  a p95 < 500 ms KPI, a fixed category list, a 1,000,000 EUR cap, a 2-decimal rule.
- Capability detection missed persistence (idea says "list by employee and month", no keyword): no database in
  the stack, the agent used an in-memory store. Acceptable for POC, wrong for MVP (ROADMAP P3-9).

### LLM judge findings (advisory, off by default)
- Haiku as judge, same spec, default thinking: $0.08, fidelity 3 after the rubric was sharpened (caught the
  invented requirements). Thinking budget 3000: $0.05, fidelity 4. Budget 0: $0.02, fidelity 5, **missed them**.
  Conclusion: cheap judging is a false economy; judge thinking left at the model default.
- 93% of a judge call's cost was hidden thinking tokens (11k of 12k output tokens), not input or tools.
- Quote grounding rejected valid evidence because judges stitch passages with "..."; now every fragment must
  occur in the artifact, in order. A previously "unreliable" real answer re-scores as pass (4.7).
- n = 1 per setting: indicative, not statistically sound. Next step is a calibration set (see Next).

## Not verified yet
- CI workflow (needs a push; a push needs Antoine's go-ahead).
- Runs with the judge enabled end to end (`judge = true`) against the real model.
- Claudo integration (Phase 1): `factory export` was never run through Claudo's orchestrator.

## Open risks / known limits
- Roles are self-declared (`--as it`): no identity or signature until P1-6.
- Keyword capability detection (see above).
- Judge n = 1 calibration; a judge that rubber-stamps would erode trust in checkpoints.
- Claudo's `just gate-ci` is red on native Windows (`fcntl` in orchestrate.py): Phase 1 starts there.
- Only one golden path (python-fastapi).

## Next step
Phase 1, P1-1: port Claudo's `fcntl` lock to a cross-platform lock in `AI-Workflow-gates/_build`
(separate repo), get its `just gate-ci` green on Windows, then P1-2 onward (see ROADMAP).
