# PROGRESS

Plan: [ROADMAP.md](ROADMAP.md). Strategy: [PLANS.md](PLANS.md). How to run: [README.md](README.md).

## State (2026-10-09, after the audit hardening)

**The 2026-10-09 audit is worked through: H1-H7 done** ([HARDENING.md](HARDENING.md)); every one of its 178
findings has a line in [docs/audits/2026-10-09-triage.md](docs/audits/2026-10-09-triage.md) (177 fixed with a
test, A142 deferred). The critical one is closed and verified live in the real container: the app's `.git` is
read-only inside it, and host git runs no hook, fsmonitor or unknown config (the control with the old mount showed
the planted hook and fsmonitor really executing on Windows).

**Verification command:** `just check` (ruff, format, pytest in parallel with 100% line and branch coverage of
`src/factory` and `scripts/`, enforced). Last run (2026-10-09, local, exit code checked): **972 passed, 2 skipped,
coverage 100.00%**. `scripts/golden_paths_ci.py --docker` passed here (three templates scaffolded, tested and
built). Claudo: `just gate-ci` green (220 + 1084) at `a6d83c1`.

**Pushed:** factory up to `4827d6b`, Claudo up to `ca576da` on `portable` (both CI green). **Local only, waiting
for Antoine's go:** every factory commit after `4827d6b` (the plan `eb540f9`, then the hardening), and Claudo
`c843e37`, `a7bd16b`, `a6d83c1`, `5ccdfd1`. Push Claudo first: the factory's sandbox labels its containers for reaping.

Published (private) since 2026-10-07: [AI-factory](https://github.com/antoinelef23/AI-factory) and
[Claudo-portable](https://github.com/antoinelef23/Claudo-portable). Claudo's `origin`
(`antoinelef23/AI-Workflow-gates`) is never pushed to: `remote.pushDefault = portable`.

### 2026-10-09 (Opus): identity, dependency policy
- **Known limit solved, verified identity** (`[identity] provider = "github"`, IT-owned `roles.toml`, optional
  `four_eyes`): live-checked with the real gh login (approval recorded as `antoinelef23` / `github`; a role not held
  and an impersonation refused).
- **P3-3 / P3-4 dependency policy**: gate `dependencies` (MVP+) checks `uv.lock` against radar `version`
  constraints and installed licences against `[licenses] forbidden`. Checked on the real V-1 app: 25 locked
  packages, 24 licences read, compliant; it first exposed a design bug (FastAPI's constraint applied to uvicorn),
  fixed: a constraint pins the package named like the entry's id.
- **P2-5 GitHub issue intake**: `factory inbox` imports open issues labelled `factory` from `[intake] repo` (maturity
  from a label or a `Maturity:` line, author as requester, business role required when identity is on), comments
  on receipt and on every stage change. Live smoke on the private `v2-app-ping-service`: issue #2 imported, receipt
  and "waiting for business" comments posted, issue closed afterwards.
- **P3-9 capability analyst**: at triage an LLM (`models.triage`) says which optional capabilities the idea needs,
  each with a verbatim quote of the idea (checked; ungrounded or unknown claims dropped); keywords stay the floor,
  failures fall back to them, the technology is still chosen by the radar. Live: the live1 idea (keywords missed
  its database) got `database` and `frontend`, both grounded, $0.017.
- **P1-7 sandbox by default**: every agent build runs in Claudo's container (`deploy/sandbox/setup.sh`: images, an
  `--internal` network, a deny-by-default tinyproxy allowing api.anthropic.com, PyPI and npm). Contained: the
  factory's build agent, Claudo's agents (`LAB_RUNNER=sandbox`) and, new in Claudo 76465c3, the code they write
  (task verify, `just evals`); the factory's test/lint gates too. Found and fixed on the way: docker mounts a tmpfs
  `noexec`, so every contained `uv run pytest` failed with EACCES. Live (Docker Desktop 29.8): secret absent, host
  fs unreachable, LAB mount read-only, no direct internet, PyPI via the proxy, example.com refused (403 Filtered);
  the fullstack app's real gates (pytest 1 + npm ci + vitest 3) and the worker's (4) pass in the container and the
  dependencies gate reads the licences it installed; Claudo's real `_run_verify` passes contained, and its
  secret-absence test fails on the host as the control. The PR body says where the agent's code ran.
- **First billed build in the sandbox** (2026-10-09, scratch live8, `utc-clock` MVP, $2.17 in all; smoke first:
  one sandboxed call, $0.0006). Spec, design and plan on the host; Claudo's 3 tasks, its Opus reviewer, the fix
  agent and all gates in containers whose only traffic was api.anthropic.com through the proxy. It found two
  bugs, both fixed: (1) from a Windows host every mounted file is 0777 and ruff 0.16 enables EXE rules by
  default, so the lint gate failed on every file, unfixably (now: checks run through `lab-ws` on a copy with
  git's modes, Claudo ca576da); (2) a targeted fix after Claudo's run was never committed, so `clean_tree` failed
  every retry (now its own commit + a `fixed_after_review` ack, pre-existing bug). Result: 8/8 gates pass; the
  reviewer's BLOCK (9 real ruff 0.16 lint errors) predates the fix commit that cleared exactly those 9.
- **P3-8 golden paths**: each template carries a `golden.toml` (capabilities, radar techs, gate overrides); the
  factory picks the one covering the idea's optional needs among those whose techs the radar allows. New:
  `fullstack-react` (FastAPI + React/Vite/TypeScript, pytest + vitest, its tests gate also runs `npm ci` + vitest)
  and `python-worker` (broker-agnostic consumer + the mandated `/health`). Node.js, Vite and Vitest added to the
  radar. Live: both scaffolded through the factory and their real gates passed (backend 1 + vitest 3; worker 4).

### After V-1/V-2 (2026-10-09, Opus): the live findings fixed, the judge as a panel
- **Plan restore** (finding 1): the `## Run log` section is bookkeeping, but only its table rows, blank lines and
  intro: a task written there is still plan (hashed, restored). An agent's untimed row no longer fails the immutable
  gate; a real edit of the plan inside the app is put back before the gates, without an agent run, run log kept, and
  becomes a `plan_edited` acknowledgement for IT; retries no longer erase Claudo's rows.
- **Invented KPIs** (finding 2): both spec prompts allow only the targets the idea gives.
- **Judge context** (finding 3): a change's artifacts are judged as a delta; new-app mandates do not apply.
- **Judge panel**: `judge_votes = 3` (median per criterion, unreliable runs do not vote, every vote in the report);
  the billed calibration can run as a panel (`JUDGE_VOTES=3`).
- **Panel measured (2026-10-09, billed, $0.73 for two runs):** 3 votes caught 3/4; the miss (`no_evals`) was
  structural: a missing section cannot be quoted, so its score was discarded. With checked absence claims
  (`ABSENT: <element>`, accepted only if the element really does not occur and the score is 1-2): **4/4
  caught**, good spec not failed, about-endpoint case `revise` (fidelity 3).
Verified: `just check` 561 passed, 2 skipped (free; no billed call in this step).

### V-1 / V-2: the corrected factory on real Claudo and real GitHub (2026-10-08, Antoine's go)
Scratch factory `live7` from frozen snapshots; `budget_usd = 3` per orchestrator run; judge on from MVP.
- **App `ping-service` (MVP): $1.72.** Claudo PASS, all 7 gates, diff exactly the idea, `uv.lock` in the scaffold
  commit, only bookkeeping leftovers. IT's approval resumed Claudo with `review_reused` (one paid review, not two).
- **Change `about-endpoint`: $2.72.** The spec kept trailing slashes and near-miss paths as NON-goals (S-1 worked);
  the diff only adds `GET /about` and tests. Claudo BLOCKed on one lint error that the factory's lint gate had also
  caught and fixed by retry; Antoine chose reject-for-rework. That rejection was LOST (race below, now fixed); re-sent
  on the fixed Claudo it reworked T2 (`checkpoint_rejected` in the journal), the review was reused (tree unchanged),
  IT approved, the resume reused the review again: shipped with no extra cost.
- **V-2:** published as PRIVATE `antoinelef23/v2-app-ping-service` (the old `app-ping-service` is kept until
  Antoine deletes it, hence the `v2-app-` prefix) and PR #1 opened: body with gates, PASS, approvals, cost; no
  approval token among the files. The old PR #1 of `app-ping-service` was closed by `factory abandon` (comment +
  branch deleted): C8 verified on real GitHub. On 2026-10-09 Antoine squash-merged PR #1 on GitHub: `factory
  sync about-endpoint` fast-forwarded the local app (local main = origin/main, change branch gone, 10 tests
  pass) and read the PR's CI back as `pass` (the golden path's workflow); `factory drift`: 1 compliant, 0
  drifted. **V-2 done**: delivery is verified end to end on real GitHub. The old `app-ping-service` is deleted.
- Earlier attempts in `live6` ($1.08) stopped on two factory contradictions, both fixed: the judge rubric demanded an
  edge case S-1 forbids; the spec mandated an exact `/health` body IT's golden path does not return (Antoine: subset).
- **Found and fixed live:** Claudo printed "waiting" before honouring a pending rejection, and the factory stops the
  orchestrator on that line, so a rejection could be consumed and lost (Claudo `4546155`, regression test that fails
  on the old code, pushed, CI green).
- **Found, not fixed yet (next):** (1) during the rework the implementer wrote its own untimed row into the approved
  `tasks.md`; the immutable gate rightly failed, and the retry "fixed" it by recommitting the approved triplet, which
  also erases Claudo's legitimate run-log rows and spent an agent run ($0.10) on a non-code failure. Restore the
  approved plan deterministically (keep run-log rows, no agent). (2) The spec writer keeps inventing a "30 requests /
  500 ms" KPI (judge: fidelity 3). (3) The judge faults a CHANGE spec for lacking the new-app `/health` mandate: it is
  not told the artifact is a change.

### Progress after the corrections (2026-10-08, Opus)
- **P3-1 Backstage radar import**: `factory radar-import tech-radar.json` reads Backstage's tech-radar plugin JSON
  (newest timeline move = ring, company ring/quadrant ids resolved through their names, `key` as alias, optional
  `category` / `replacedBy`), with the same validation as the CSV path. 7 tests (`tests/test_importer.py`).
- **P2-4 CI read back**: `factory sync` records the pull request's CI (pass | fail | pending | none, failing check
  names), `show` displays it, and a merge made on the host over failing CI is logged `merged_over_red_ci`. The
  factory still never blocks a host merge (branch protection is the place for that). 9 tests, `gh` adapter
  included (`gh pr checks` exits 1/8 for failing/pending: the JSON is read anyway). Not run against real GitHub.

- **Judge rubric vs S-1 (found by the V-1 live run):** the `examples` criterion demanded "at least one edge
  case" while S-1 forbids invented ones, so the judge failed the first V-1 spec (examples 2/5). New wording: a
  dedicated examples section, one per behaviour, edge cases only where the idea or spec define them. Billed
  recalibration with sonnet (Antoine's go, about $0.55 in all): first wording 3/4 caught, 0 missed, but it no longer
  separated "examples" from "none" in a targeted re-check; final wording: both targeted re-checks fail the
  no-examples spec (examples 1/5) and keep the good one at "revise"; one full run then gave 2/4 caught, `no_evals`
  missed (its verdict still fell to fail), `no_examples` inconclusive (the judge cannot quote a missing section,
  so the score is ungrounded), the about-endpoint answer unreliable. **The judge is noisy run to run**: one
  calibration run is not a measure. Next: several runs or a majority vote per artifact before trusting a single
  verdict; it stays advisory.

### The 2026-10-08 review and its corrections
Opus reviewed everything Sonnet built (CORRECTIONS.md has the findings with their evidence, and the plan). Sonnet
then applied the plan. Status per item:

| Phase | Items | Status |
|---|---|---|
| 1 security | C2-a tokens never committed; C2-b per-round nonce (Claudo + bridge); C3-sec secret out of the factory tree | done, tested |
| 2 ships what was approved | C3 scope drift committed apart + IT acknowledgement; C4 nothing ungated after approval, moved verdict goes back to IT (Claudo reuses an unchanged review); C5 publish only the approved commit, history secret scan | done, tested |
| 3 delivery | C6 crash-safe publish; C7 one merge path; C8 abandon closes the PR; D-1..D-4 | done, tested |
| 4 gates | G-1 unparseable manifest blocks; G-2 secrets coverage; G-3 git-tracked files scanned; G-4 rewritten tests; G-5 plan frozen; G-6 plan scope check | done, tested |
| 5 spec / process | S-1 spec prompts + `judge_from` (default off) + calibration case; P-1 live-run protocol in CLAUDE.md; L-1 report path; L-5 Run log heading | done, tested (the billed judge test was NOT run) |
| 6 docs | L-2 this file; L-3 README; L-4 ROADMAP | done |
| judge review (section 4) | J-1 rejection after a re-decision reworks; J-2 review reuse keyed on the reviewed tree (Claudo); J-3 refusals name `factory reject`/`factory change`; J-4 history scan labels + merges; J-5 runtime ignores in `.git/info/exclude`; J-6 scope heuristic warns, never blocks (+ the clause regex held backspace bytes); J-7 nonce for items paused before the upgrade; J-8 older Claudo reported; J-9 abandon checks the tree first; J-10 weak tests replaced; J-11 unbound tokens flagged; J-12 committed dependencies not scanned | done, tested (Opus) |
| process | J-13: the red commit and its fix were squashed into one green commit before the push (Antoine's go) | done |
| outward | O-1 close PR #1 and abandon the change; O-2 delete the smoke repo | **waiting for Antoine** (O-3, the scratch secret, is done) |
| billed | V-1 re-run the change through Claudo; V-2 real GitHub merge + `sync` | **waiting for Antoine** |

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
| P1-7 sandbox by default | done | agent builds, Claudo's verify/evals and the factory's gates run in Claudo's container (internal network + allowlist proxy); MVP+ blocks when it is not ready unless `--unsafe-host` (IT ack); POV/POC falls back to the host with a note (D5); verified live, including a billed MVP build in the sandbox (below) |
| P1-10 | open | see Next |

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

### Live change run through real Claudo (2026-10-08, shipped MVP app `ping-service`): what really happened
`factory change` -> spec -> design -> plan (1 plan-lint retry) -> build on `factory/about-endpoint` -> gates -> IT
approval. **$3.41**: spec+plan $0.32, Claudo build $2.29, the post-approval resume **$0.80** (a second Opus review),
3 tasks, 20 tests green, ruff clean, under the $3 per-run cap. The engine path works. **The result must not be
merged**; an earlier version of this file called the reviewer's BLOCK "real but stale" and the `--note` rule "working
as designed". That was wrong. The chain:
1. The spec invented BHV-4 (`GET /about/` -> 404), which the idea never asked for and which, under FastAPI defaults
   (307), contradicts INV-3 ("the only permitted change is that `GET /about` no longer returns 404"). -> S-1.
2. The plan's T2 listed only the test in `files_touched` while its prompt authorised editing `app/main.py`. -> G-6.
3. The implementer set `redirect_slashes=False` globally: `GET /health/` and `GET /version/` now return 404
   instead of 307. Claudo's scoped commit skipped the file. -> C3.
4. The reviewer saw the uncommitted edit (BLOCK); the factory then committed it in a `chore` commit with no spec ID;
   the trajectory gate, which only checks task commits, passed. -> C3.
5. Claudo's post-approval run re-reviewed, said WARN and named the regression explicitly; the factory showed the
   old verdict and published without gating that run. -> C4.
6. The scripted persona "Bob" approved over the BLOCK with a note that addressed "uncommitted" only. -> P-1.
7. Approval tokens were committed and pushed with the app. -> C2.
The remote artefacts: private repo `antoinelef23/app-ping-service` (PRIVATE) and its PR #1, **left open on
purpose**: close it, do not merge it (CORRECTIONS.md O-1, O-2).

### Delivery to GitHub (2026-10-08, hardened after the review)
`[delivery] provider = "github"` (off by default). `factory publish` (IT): shipped app -> private repo
`<repo_prefix><slug>`; approved change -> branch + pull request. `factory sync`: after IT merged on the host,
fast-forward the local app (ff, squash and merge-commit merges tested). By construction: no visibility setting
(`--private` hardcoded, tested), the factory never merges, never pushes into a repo it did not create, never repoints
a remote. Since the review: only the commit IT approved leaves (dirty tree, moved head and history secrets refused;
legacy items need `--accept-unverified`), the repo is recorded the moment it exists (a failed push no longer orphans
it), a published app's changes can only merge through their PR, and `abandon` closes the PR and deletes its branch.

## Not verified
- The judge enabled during a Claudo-built item (`judge = true`) end to end with real models.
- The billed judge test on the about-endpoint spec (`pytest -m live --live -k edge_case`).
- `judge_from` with real models. `abandon` and `publish --accept-unverified` against real GitHub.

## Open risks / known limits
- Text matching of technology names: an alias that is also an everyday word ("requests") produced false positives on
  real plans and business ideas. Fixed with `text_strict` (code-like contexts only) for `requests`; any new ambiguous
  radar entry needs the same flag.
- Roles: SOLVED when `[identity] provider = "github"`: the actor is the authenticated `gh` account, it must hold
  the role in IT's `roles.toml`, `--by` cannot impersonate, approvals record `verified: github`, and `four_eyes`
  keeps one person from deciding two roles of an item (verified live with the real login). Left: the default is
  still `provider = "none"` (self-declared, labelled so in PRs), and a person with shell access to the factory
  machine can edit roles.toml: keep it under review like the radar.
- Approval secret: CLOSED for sandboxed builds (2026-10-09). The agent, Claudo's verify/evals and the factory's
  gates run in a container that never receives it; measured: a test asserting the secret is absent passes in the
  sandbox and FAILS on the host (so before this, agent-written tests run by `verify` could read it). Since the
  audit (H1), code in the container can no longer reach the host through git either (read-only `.git`, hardened
  host git, IT-owned gate commands). Left: an `--unsafe-host` build (IT must acknowledge it), a POV/POC built on
  the host when Docker is down (noted on the item), and the spec/design agents, which run on the host with
  read-only tools.
- The text-strict list (`uv`, `requests`, MongoDB's `motor`) is pinned by a test: a new ambiguous radar alias needs
  an entry. `deploy/sandbox/lab-ws` (Claudo) is checked live only, not by a unit test.
- Capability detection: keywords plus, since 2026-10-09, an LLM analyst whose claims must quote the idea (live:
  the live1 idea now gets its database, $0.017). The stack is still compiled from the radar.
- Claudo's two Opus reviews dominate MVP cost (above).
- Three golden paths since 2026-10-09 (python-fastapi, fullstack-react, python-worker); more are P3-8 follow-ups.

- The Claudo reviewer still reads the tree BEFORE the factory commits leftovers. That is now harmless: what it
  missed is committed apart, flagged `scope_drift`, and IT must acknowledge it with a note.
- A dependency a build adds rewrites `uv.lock` outside every task scope: `scope_drift`, by design (new apps get
  their lock with the scaffold, so a plain `uv run` no longer does).
- The plan scope check (G-6) is a heuristic over clauses of the task prompt: it misses a path named in a clause
  without a write verb. It narrows the gap, it does not close it (C3 is the backstop).
- Claudo names a consumed token `.handled-<second>`: two checkpoints consumed in the same second would collide on
  Windows. Real resumes are minutes apart; not fixed.
- `gh` is the only git host adapter; GitLab/Bitbucket would implement the same 7-method `GitHost` protocol.
- On an app NOT built from the golden path, `prepare_project` still adds an `evals` recipe to its `justfile` and
  leaves it uncommitted (Claudo's eval gate needs `just evals`). Pre-existing since 2026-10-07; golden-path apps
  already have the recipe. Same class as J-5, not fixed: it would need a deliberate setup commit on the base.

## Next steps (in order)
1. Antoine: a go to push (Claudo `c843e37`..`5ccdfd1` to `portable` first, then the factory). CI will run
   the new golden-paths job (docker builds) for the first time on GitHub.
2. Antoine, billed (ask first): one sandboxed MVP smoke on the hardened code (about $2, as utc-clock was), and
   `just calibrate` again: the calibration baseline was corrected (A73), so the recorded numbers are stale.
3. Antoine: decide the live item `utc-clock` (scratch live8, at IT's ship review); revoke the token pasted into
   the session. Decide A142 (roles keyed on the GitHub login, or login + account id in roles.toml).
4. Then: more golden paths on demand, P3-5 (vulnerability policy), P3-10.
