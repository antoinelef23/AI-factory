# PROGRESS

Plan: [ROADMAP.md](ROADMAP.md). Strategy: [PLANS.md](PLANS.md). How to run: [README.md](README.md).

## State (2026-10-07)

**Phase 0 complete. Phase 1 core complete** (P1-1, P1-3, P1-4, P1-6). Local git history only, **nothing pushed**.
The factory now builds MVP+ apps through Claudo's orchestrator and ships them with a signed human approval.

**Verification command:** `just check` (ruff check + format check + pytest). Last run: **248 passed**.
Claudo (`AI-Workflow-gates/_build`, separate repo, 6 local commits): `just gate-ci` green on **Windows and Linux (WSL)**.

### Roadmap items done

| ID | Status | Evidence |
|---|---|---|
| P0-1..6 | done | see git log; CI workflow written but **never run** (needs a push) |
| P1-1 Claudo on Windows | done | `fcntl` lock -> `oslock`; runner finds `claude.exe`; Git-Bash `find_bash`; `LAB_PYTHON`; UTF-8 decoding; posix feature keys. 54 failing tests -> 0, Linux control run green before and after |
| P1-3 plan validated by Claudo | done | factory runs Claudo's real `--validate`; offline plan lints clean at all 4 maturities; live agent plan passed first try (10 nodes) and an independent lint agrees |
| P1-4 build through Claudo | done | live MVP item shipped: 3 tasks first attempt, 13 evals green, per-task commits |
| P1-6 signed approvals | done | live: token `approved_by=Bob`, payload bound to CP-1, consumed by Claudo; secret in `.factory/`, outside the app |
| P1-2 package engine | **replaced** | the factory *discovers* Claudo (config / `CLAUDO_HOME` / sibling) instead of vendoring it; decision D2 revised, no monorepo |
| P1-8 trajectory guard as gate | done | Claudo's `trajectory_guard.py` runs as an MVP+ gate; real guard tested on a forged journal |
| P1-9 spec/design immutable | done (factory-side) | sha256 frozen at approval; gate fails if the store's or the app's copy differs; 4 tamper tests, mutation-checked |
| P1-5 radar check on plans | done | `plan_radar_errors` feeds the lint retry loop; audited on 4 real agent plans: 0 false positives, 2 true positives (SQLite at MVP+) |
| P3-2 radar diff, P3-6 drift (partial) | done | `radar-diff`, `drift [--open]`; real CLI story verified; migration items tracked, not executable until P2-6 |
| P3-7 exceptions with expiry | done | design-review exceptions lapse after `[policy] exception_days` (180), `allow --expires --reason`; the build gate and drift ignore lapsed ones; mutation-checked |
| P3-1 radar importer (partial) | done | `radar-import` for CSV/JSON (BYOR style, `;`/tab delimiters, BOM, French ring names); validated through the factory's own loader; imported radar drives a full item |
| P1-7, P1-10 | open | see Next |

### Live runs and what they taught (all in scratch copies, not committed)

| Run | What | Result |
|---|---|---|
| live1 | single agent, POC expense API | $0.71, 3.5 min, 79 tests, gates first try |
| live3 | Claudo, POC ping service, cap $3 | **$3.29 for ONE task**, 11 min: planner told "T1 scaffolds", agent redid a working app (overwrote /health, deleted its test), then failed the eval gate (golden test not an eval). Budget cap stopped it cleanly |
| live4 | Claudo, MVP ping service, cap $4, after the fixes | **shipped, $2.37**: 3 tasks first attempt ($0.99) + 2 Opus reviews ($1.19) + spec/plan; 13 evals green; radar PASS |

Fixes born from live3: planner told the app is already scaffolded (+ file list), tasks own their tests with
`test_eval_<n>` + `@pytest.mark.eval`, golden health test is an eval, Claudo engages from `build_from = "mvp"`
(POV/POC use the single agent), Claudo spend is read from its journal into the item cost.

**Cost structure to know:** at MVP the two Opus reviewer calls are 55% of the Claudo spend. The review runs again
after IT approves (Claudo reviews before polling for the token). Candidates: cheaper reviewer for MVP, skip the
second review (needs a Claudo change).

### LLM judge (advisory, off by default)
- 93% of a judge call's cost is hidden thinking; capping thinking (3000 or 0) saved money but **missed invented
  requirements** (fidelity 5 vs 3), so the default stays at the model default (n = 1 per setting: indicative).
- Quote grounding rejected valid evidence because judges stitch passages with "..."; every fragment must now occur in
  order. Judge still never blocks.

## Not verified
- CI workflow (needs a push; a push needs Antoine's go-ahead).
- The judge enabled during a Claudo-built item (`judge = true`) end to end with real models.
- Sandbox runner (Docker) on Windows; the bind-mount path form is unit-tested only.
- Claudo reject path against the real orchestrator (`reject_checkpoint` writes the file Claudo documents; the factory
  flow is tested with a scripted engine, the live runs only exercised approve).
- Factory + Claudo on Linux (the Claudo suite was run there; the factory suite was not).

## Open risks / known limits
- Text matching of technology names: an alias that is also an everyday word ("requests") produced false positives on
  real plans and business ideas. Fixed with `text_strict` (code-like contexts only) for `requests`; any new ambiguous
  radar entry needs the same flag.
- Roles are self-declared (`--as it`): the signed token proves the factory wrote it, not who the human was.
- Approval secret is held by the orchestrator process on the host, so a host agent with code execution could read it
  (Claudo's documented residual M4; the sandbox runner is the fix, ROADMAP P1-7).
- Capability detection is keyword-based (idea with "list by employee and month" got no database in live1).
- Claudo's two Opus reviews dominate MVP cost (above).
- Only one golden path (python-fastapi).

## Next steps (in order)
1. P1-7: sandbox runner by default for MVP+ builds. NOT a quick win: Claudo's sandbox needs an egress-allowlist proxy
   that does not exist yet (its own backlog) and Docker is not running here; needs a decision and a setup session.
2. P2-6 change an existing app (unblocks migration items); radar importers for Backstage/other formats.
3. Ask Antoine before spending more: a live run through the reject path (~$3-4) and `judge = true` end to end (~$0.5).
4. Phase 2 (real delivery: git repo, PR, GitHub issue intake) needs decisions D3/D6 and a push go-ahead.
