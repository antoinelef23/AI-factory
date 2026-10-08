# PROGRESS

Plan: [ROADMAP.md](ROADMAP.md). Strategy: [PLANS.md](PLANS.md). How to run: [README.md](README.md).

## State (2026-10-08)

**Phase 0 complete. Phase 1 core complete** (P1-1, P1-3, P1-4, P1-6).
Published (private) on 2026-10-07: [AI-factory](https://github.com/antoinelef23/AI-factory) and
[Claudo-portable](https://github.com/antoinelef23/Claudo-portable) (Claudo + the portability work). The original
Claudo repo (`antoinelef23/AI-Workflow-gates`, local remote `origin`) was deliberately NOT pushed to: Claudo's local
branch tracks `portable` and `remote.pushDefault = portable` so a plain `git push` cannot reach it.
**CI is green** on GitHub (Windows + Ubuntu for the factory; Claudo's own gate workflow on Linux).
The factory now builds MVP+ apps through Claudo's orchestrator and ships them with a signed human approval.

**Verification command:** `just check` (ruff check + format check + pytest). Last run: **402 passed, 1 skipped** (delivery added 23) (also green in GitHub CI on Windows and Ubuntu; the real-engine integration tests skip there because no Claudo checkout is present) (the skipped one is the billed calibration, `just calibrate`).
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
| P2-6 change an existing app | done | `change`/`merge`/`abandon`; branch `factory/<slug>`, fast-forward-only merge by IT; migrations from `drift --open` are runnable; full story tested with an agent that really edits the app (migrate -> approve -> merge -> drift clean); fast-forward rule mutation-checked |
| Spec structural lint | done | closes the judge's measured blind spot (missing evals table): `lint_spec` + re-prompt loop; 0 false positives on 2 real agent specs; it found a real hole in the offline template (BHV-2 had no eval); coverage rule mutation-checked |
| README as executable docs | done | tests run the README's offline walkthrough as written, check every command it names exists and every command is documented; found `board` undocumented; mutation-checked |
| P1-7, P1-10 | open | see Next |

### Live runs and what they taught (all in scratch copies, not committed)

| Run | What | Result |
|---|---|---|
| live1 | single agent, POC expense API | $0.71, 3.5 min, 79 tests, gates first try |
| live3 | Claudo, POC ping service, cap $3 | **$3.29 for ONE task**, 11 min: planner told "T1 scaffolds", agent redid a working app (overwrote /health, deleted its test), then failed the eval gate (golden test not an eval). Budget cap stopped it cleanly |
| live5 | Claudo, MVP, **IT rejects at ship review**, rework, approve; cap $3 per run | **works end to end, $4.98** (above my $4 estimate): rejection file consumed by Claudo, T3 reworked, signed token consumed, item shipped. It exposed 3 real gaps, fixed below |
| live4 | Claudo, MVP ping service, cap $4, after the fixes | **shipped, $2.37**: 3 tasks first attempt ($0.99) + 2 Opus reviews ($1.19) + spec/plan; 13 evals green; radar PASS |

Fixes born from live3: planner told the app is already scaffolded (+ file list), tasks own their tests with
`test_eval_<n>` + `@pytest.mark.eval`, golden health test is an eval, Claudo engages from `build_from = "mvp"`
(POV/POC use the single agent), Claudo spend is read from its journal into the item cost.

**Cost structure to know:** at MVP the two Opus reviewer calls are 55% of the Claudo spend. The review runs again
after IT approves (Claudo reviews before polling for the token). Candidates: cheaper reviewer for MVP, skip the
second review (needs a Claudo change).

### What the reject-path run (live5) exposed, and the fixes
Claudo's Opus reviewer returned **BLOCK** after the rework and IT approved anyway; reading its report showed why:
1. The rework (docstrings, README) was **never committed**: Claudo's scoped commits skip files outside a task's
   `files_touched`. Every gate judged the working tree, but what is delivered (and what a PR will contain) is HEAD.
   Fix: leftovers are committed in a separate labelled commit after each Claudo run, and a `clean_tree` gate (MVP+)
   fails on anything uncommitted. Tests also caught a bug in the helper (stripped `status --porcelain` output).
2. **HEAD failed its own merge gate**: the golden path ships `pydantic`, design section 3 never listed it, so the
   agent-written eval allowlist rejected it. Fix: design section 3 now lists the golden path dependencies as
   pre-approved, and the planner is told evals must allow them.
3. **IT approved a BLOCK blind**: the factory never showed Claudo's reviewer verdict. Fix: verdict recorded and shown
   by `show`; approving over anything but PASS requires `--note`, recorded with the approval.
The three fixes are covered by unit tests (the real BLOCK report is a fixture) but were NOT re-run live (~$5).

### LLM judge (advisory, off by default): calibrated
`just calibrate` (billed, ~$0.2) scores a known-good, agent-written spec against 4 degraded copies (no evals, no
examples, an invented requirement, vague outcomes). Findings:
- First run exposed a contradiction in MY system: the judge failed the good spec for "inventing /health", which the
  factory's own spec prompt mandates. The fidelity rubric now exempts the two mandated additions (regression test).
- haiku (original default): answers frequently **unreliable** (ungrounded quotes); run B: 0 conclusive. Earlier
  runs: caught some, missed others, noisy.
- sonnet: **caught 4/4 (run A) and 3/4 (run B)** for $0.05-0.17 per 5 calls (cheaper than haiku, it thinks less).
  Both models are weakest at noticing a *missing* evals table: structure is better checked deterministically.
- Decision: default judge = **sonnet**. Caveat, untested: it is also a generator (self-preference bias).
- Measurement lessons: a missing baseline score gave a bogus drop (now "inconclusive"); n = 2 per model is still thin.
- Cost mechanics: 93% of a haiku judge call was hidden thinking; capping thinking missed invented requirements.
- Quote grounding rejected valid evidence when judges stitch passages with "..."; now every fragment must occur in order.

### Live change run through real Claudo (2026-10-08, on a shipped MVP app `ping-service`)
`factory change` -> spec -> design -> plan (1 plan-lint retry) -> build on branch `factory/about-endpoint` ->
gates -> IT approval. **$3.41 total** (cap `budget_usd = 3` per orchestrator run; build $2.61, the approval step's
Claudo resume $0.80), 3 tasks, 20 tests green on HEAD, ruff clean. What it showed:
1. The change flow works end to end with the real engine: scoped per-task commits, signed approval, no cap breach.
2. **Claudo's reviewer returned BLOCK and the BLOCK was real but stale**: the build agent edited `app/main.py`
   (`redirect_slashes=False`, needed by its own eval) outside every task's `files_touched`, so it was uncommitted
   when the reviewer ran. The factory's leftovers commit put it on HEAD afterwards, and HEAD was verified clean (20
   passed). IT approved with `--note` explaining exactly that, which is the `--note` rule working as designed. The
   reviewer runs BEFORE the leftovers commit, so a stale BLOCK can recur; see open risks.
3. Delivery smoke on real GitHub: `publish` created PRIVATE `antoinelef23/app-ping-service` (GitHub reports
   `PRIVATE`) and pushed `main` while the change was in flight; `publish` of the change opened PR #1
   (`factory/about-endpoint` -> `main`) whose body shows intent, gates, the BLOCK verdict, approvals + IT's note.
   The PR was left OPEN for IT to merge; `sync` reports OPEN. The merge + `sync` path is covered by tests with a
   local bare repo but NOT yet run against real GitHub.

### Delivery to GitHub (done 2026-10-08)
`[delivery] provider = "github"` (off by default). `factory publish` (IT): shipped app -> private repo
`<repo_prefix><slug>`, base branch pushed; approved change -> branch pushed + pull request. `factory sync`: after IT
merged the PR on the host, fast-forward the local app (ff-only, refuses a diverged base). By construction: no
visibility setting (`--private` hardcoded, tested), the factory never merges, refuses to push into a repo it did not
create, refuses to repoint an existing remote, local `merge` is refused once a PR exists.

## Not verified
- The judge enabled during a Claudo-built item (`judge = true`) end to end with real models.
- Sandbox runner (Docker) on Windows; the bind-mount path form is unit-tested only.
- `sync` after a REAL merge on GitHub (PR #1 of `app-ping-service` is open; merge it, then `factory sync about-endpoint`).

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

- The Claudo reviewer reads the tree BEFORE the factory's leftovers commit, so it can BLOCK on work that is
  committed a moment later (seen live). Possible fix: commit leftovers before the reviewer panel runs.
- `gh` is the only git host adapter; GitLab/Bitbucket would implement the same 5-method `GitHost` protocol.

## Next steps (in order)
1. P1-7: sandbox runner by default for MVP+ builds. NOT a quick win: Claudo's sandbox needs an egress-allowlist proxy
   that does not exist yet (its own backlog) and Docker is not running here; needs a decision and a setup session.
2. Radar importers for Backstage/other formats; P2-5 GitHub issue intake (delivery itself and the live change run are done).
3. Ask Antoine before spending more: a live run through the reject path (~$3-4) and `judge = true` end to end (~$0.5).
4. Merge PR #1 of `app-ping-service` yourself, then `factory sync about-endpoint` to close the loop on real GitHub.
