# AI Software Factory (MVP)

Business ideas in, production-grade apps out, **governed by the IT tech radar**.

| Actor | Does | Command |
|---|---|---|
| Business | submits an idea, validates the spec | `factory intake`, `factory approve --as business` |
| IT | owns `radar.toml` + golden paths, validates design exceptions, merges | `factory approve --as it`, `factory allow`, `factory promote`, `factory check` |
| Owner | approves the execution plan | `factory approve --as owner` |
| Factory | spec -> design (from the radar) -> plan -> build -> gates | `factory run` |

## How it works

```
idea --> triage --> spec --> [business] --> design --> [IT*] --> plan --> [owner] --> build --> gates --> [IT merge] --> shipped
                                             ^ compiled from radar.toml          golden path + agent   radar, secrets, tests, lint
```

- **The stack is compiled from the tech radar, never chosen by an LLM.** Business asks for MongoDB (hold)?
  The factory notes it and uses PostgreSQL (adopt) instead.
- **Ring x maturity policy** (`src/factory/radar.py`). The higher the rung, the stricter:

  | ring | pov | poc | mvp | prod |
  |---|---|---|---|---|
  | adopt | allow | allow | allow | allow |
  | trial | allow | allow | IT approval | IT approval |
  | assess | allow | IT approval | block | block |
  | hold | block | block | block | block |
  | not on radar | IT approval | IT approval | IT approval | block |

- `*` IT design review is skipped for POV/POC when the whole stack is allowed. From MVP up it always happens.
- **Gates per maturity** (`factory.toml [gates]`): radar guard (manifests, imports, Docker images, design doc),
  secrets scan, tests, lint. A failed gate blocks the item and feeds the report to the next build attempt.
  The radar guard analyses Python (`pyproject.toml` incl. Poetry, `requirements*.txt`) and JavaScript manifests; a
  manifest it cannot parse **blocks**, and one from an ecosystem it does not analyse (`go.mod`, `pom.xml`,
  `build.gradle`, `Cargo.toml`, `Gemfile`, `composer.json`) needs IT's review. In a git project it scans everything
  git tracks, whatever the folder is called.
- **Apps start from IT golden paths** (`golden_paths/`), so the CI, Dockerfile and layout are IT's, not the agent's.
- Artifacts use Claudo's triplet format (`spec.md`, `design.md`, `tasks.md`) and travel with the app in
  `apps/<slug>/work/<slug>/`. `factory export` hands them to a Claudo project.

## Setup (once)

Needs `uv` (and optionally `just`). Open a **new** terminal so `uv` is on PATH, then:

```powershell
cd C:\Users\Antoine\Projets\AI-factory
uv sync
just check          # verification: lint + format + tests (offline, ~5 s)
```

## Test it tonight

### 0. The 5-second proof (free)

```powershell
uv run factory demo      # self-checking: MongoDB replaced, Flask caught, Django escalated to IT, merge stays human
```

### 1. Offline run (free, about 1 minute)

```powershell
uv run factory radar
uv run factory intake "Customer callback log" --maturity poc --idea "A web page where support agents record customer callbacks and track their history. Store it in MongoDB."
uv run factory run customer-callback-log                       # -> waits for business (note: MongoDB replaced)
uv run factory approve customer-callback-log --as business --by Alice
uv run factory approve customer-callback-log --as owner --by Antoine   # build + gates run here
uv run factory approve customer-callback-log --as it --by Bob          # ship
uv run factory show customer-callback-log                      # full history
uv run factory board                                           # every work item, stage and who it waits on
```

Look at `work/customer-callback-log/` (idea, spec, design, tasks, gate report) and
`apps/customer-callback-log/` (the app). Run it with `cd apps/customer-callback-log; uv run uvicorn app.main:app`.

Offline mode writes template specs and only scaffolds the golden path: it shows the governance flow, not real code.

### 2. Real agents (Claude Code, billed: roughly $0.5 to $3 per item with Sonnet)

```powershell
uv run factory intake "Expense tracker" --maturity poc --idea "An API where employees submit expenses (amount in EUR, category, date) and managers list them by employee and month."
uv run factory run expense-tracker --runner claude
uv run factory reject expense-tracker --as business --reason "Add a rule: expenses above 500 EUR need a receipt URL"
uv run factory run expense-tracker --runner claude
uv run factory approve expense-tracker --as business --runner claude
uv run factory approve expense-tracker --as owner --runner claude       # Claude builds in apps/expense-tracker
uv run factory approve expense-tracker --as it
```

### 3. Things to try

- **Governance:** `--maturity mvp --idea "... built with Django"`. Django is *trial*, so IT design review is
  required, and approving it records an IT exception.
- **Catch a cheat:** after a build, add `"flask"` to `apps/<slug>/pyproject.toml`, then `uv run factory run <slug>`
  in a blocked state, or run `uv run factory check apps/<slug> --maturity poc`. Flask is on hold, so it fails.
- **Audit any repo:** `uv run factory check C:\path\to\any\repo --maturity prod`.
- **Promotion:** `uv run factory promote <slug> --to mvp --as it` sends the app back through design with stricter rules.
- **IT changes the radar:** move `fastapi` to `hold` in `radar.toml` and re-run `factory check apps/<slug>`.
- **Claudo handoff:** `uv run factory export <slug> --claudo C:\Users\Antoine\Projets\AI-Workflow-gates\_build`.

## Change an existing app (feature, bug, migration)

```powershell
uv run factory change orders "Add CSV export" --idea "Managers can export a month as CSV" --kind feature
uv run factory run add-csv-export                       # spec -> design -> plan -> build -> gates, as for a new app
uv run factory approve add-csv-export --as business ...   # same checkpoints; IT reviews from MVP up
uv run factory merge add-csv-export --as it             # IT's own act: fast-forward only. The factory never merges.
uv run factory abandon add-csv-export --as owner --reason "no longer needed"
```

The change works on a **branch `factory/<slug>` of the app's own git repo** and inherits the app's maturity. Its
design is the app's *existing* stack judged by the *current* radar (a migration's design lists what to remove, and
the build gates fail until the forbidden technology is really gone). Approval means "ready": the app folder keeps
showing the change branch until IT merges, so `drift` marks that app *in flight* instead of judging it, but keeps
failing while a *migration* is pending, since the violation is still there. One open change per app.
`factory drift --open` now opens migrations that you can simply `run`.

## Deliver to GitHub: one private repo per app, one pull request per change

Off by default. Set `[delivery] provider = "github"` in `factory.toml` (needs the `gh` CLI, already logged in):

```powershell
uv run factory publish orders --as it                    # shipped app -> a new PRIVATE repo app-orders, HEAD pushed
uv run factory publish add-csv-export --as it            # approved change -> branch pushed + a pull request
uv run factory sync add-csv-export                       # after IT merged the PR on GitHub: update the local app
```

Safety is built in rather than configured: the only way the factory creates a repository is `--private` (there is
no visibility setting), it **never merges** (the pull request is IT's to merge on GitHub), it refuses to push into
a repository that already exists under that name, and nothing leaves the machine unless IT runs `publish`.

What leaves is **exactly the commit IT approved**: the gates record the commit they judged, IT's approval is
refused if the app moved since and records the approved commit, and `publish` refuses a dirty tree, a tip that
differs from the approved commit, and any secret in the lines the pushed commits add (a key deleted by a later
commit is still caught). Items approved before this existed need `publish --accept-unverified`. The pull request
carries the intent, gate results, the Claudo reviewer's verdict, what IT had to acknowledge, the approvals and the
cost.

Once an app is published, its changes can only be merged through their pull request: the local `factory merge` is
refused (it would diverge from the host). `factory abandon` closes the change's open pull request and deletes its
remote branch; a pull request already merged on the host is `factory sync`'s, not abandon's. A failed push does not
orphan the repository: it is recorded the moment it is created, and the next `publish` reuses it.

## Bring your own radar

```powershell
uv run factory radar-import company-radar.csv --company "Acme" --version 2026.10   # writes radar.imported.toml
```

Reads a Thoughtworks-BYOR-style CSV or JSON (comma, semicolon or tab; Excel's BOM; ring names in English or
French: Adopter / Essayer / Évaluer / Suspendre). It never overwrites your radar without `--force`, and it tells you
what an export cannot know: each technology's **capability category** (backend, database, ...), which the design
compiler needs to *choose* a stack. Add a `category` column (and `replaced_by` for hold technologies) for full effect.

It also reads the JSON of **Backstage's tech-radar plugin** (`quadrants`, `rings`, `entries`):
`uv run factory radar-import tech-radar.json`. Each entry's ring is its most recent timeline move; your own ring and
quadrant ids are resolved through their names; an entry's `key` becomes an alias. Backstage has no capability
category either: add `category` (and `replacedBy` for hold entries) to your entries, or fill them in afterwards.

## When IT changes the radar: diff and drift

```powershell
uv run factory radar-diff old-radar.toml      # what changed, and what it newly forbids at each maturity
uv run factory drift                          # which SHIPPED apps no longer comply (exit 1 if any: CI-friendly)
uv run factory drift --open                   # ...and track a migration item per drifted app (idempotent)
```

Exceptions are debts: those granted at a design review lapse after `[policy] exception_days` (180 by default),
`factory allow <slug> <tech> --as it --reason "..." --expires 2026-12-31` sets its own date and keeps the reason.
Once an exception lapses, the build gate and `drift` stop sheltering its technology.

`drift` re-checks every shipped app (manifests, imports, Docker images, design) against the **current** radar,
honoring the exceptions IT granted per app. Migration items are tracked, not executed: changing an existing app is
ROADMAP P2-6, and `run` refuses to push them through the new-app pipeline.

## Claudo: plan validation, audited builds, signed approvals

The factory finds a [Claudo](https://github.com/antoinelef23/Claudo) checkout on its own (`[engine] claudo`
in `factory.toml`, else `$CLAUDO_HOME`, else a sibling `../AI-Workflow-gates/_build` or `../Claudo`) and then:

| Stage | What Claudo adds |
|---|---|
| plan | Every `tasks.md` is checked by **Claudo's own plan-lint** (the factory does not re-implement it). An agent plan that fails is re-prompted with the lint errors (`plan_lint_retries`), then blocked. |
| build (MVP and above) | The orchestrator runs the approved plan **task by task**: dependency DAG, per-task verify, eval gate, reviewer panel, one scoped git commit per task. Below `build_from` (default `mvp`) a single agent builds: see costs. |
| ship | The factory stops the orchestrator where it pauses for its human checkpoint, runs its own gates, and parks the item at IT's ship review. IT's approval writes a **signed (HMAC) token** carrying the approver's name, bound to a per-round nonce so a token left in a tree cannot be replayed (this needs Claudo `b9b96c7` or later; with an older Claudo the factory puts a `replay protection unavailable` note on the item). The secret lives in your per-user state directory (`%LOCALAPPDATA%\ai-factory` or `$XDG_STATE_HOME/ai-factory`), outside the factory and every app, and is withheld from the orchestrator's agents. An agent that can only write files cannot self-approve. **Residual risk:** an agent that can run arbitrary code as your user could read that secret (Claudo's documented M4); only the sandbox (ROADMAP P1-7) closes it. After the approval Claudo may change nothing but its own bookkeeping, or the ship is blocked. Anything a build edited outside every task's scope, and any existing test it rewrote, is committed apart and needs IT's `--note`. |

No Claudo found: the factory still runs, plans are simply not linted and everything builds with the single agent.

## Spec lint (deterministic, before any human)

Every spec must pass a structural lint: all seven sections, every invariant and behavior covered by an eval in the
`Covers` column, IDs unique, and what the factory mandates (the radar invariant, `GET /health` for a new app, "no
regression" for a change). An agent-written spec that fails is re-prompted with the errors (`[policy] spec_lint_retries`,
default 2), then blocked. It exists because the judge below is weakest at noticing what is *missing*; it deliberately
does not judge meaning (an invented requirement that has its own eval is structurally flawless).

## LLM judge (advisory)

`[agent] judge = true` (billed) scores each spec, plan and build against a rubric. It **never blocks**: it tells the
human reviewer where an artifact looks weak. Every score must quote the artifact verbatim (checked by code,
ungrounded scores are discarded) and the verdict is computed from the scores. It is **calibrated**: `just calibrate`
(billed, ~$0.2) scores a known-good spec against four deliberately degraded copies. Result so far: sonnet caught 7 of 8,
haiku's answers were unreliable (ungrounded quotes), so the default judge is sonnet. It is weakest at noticing a
*missing* evals table (deterministic checks cover structure better), and it shares a model family with the generators.

`uv run factory judge <slug> --kind spec|plan|build` runs it on demand.

## What it costs (measured, Sonnet generators)

| Path | Result |
|---|---|
| single agent, whole 7-module expense API (POC) | **$0.71**, ~3.5 min, gates green first try |
| plan written by an agent | ~$0.15, passes Claudo's plan-lint first try |
| judge call (sonnet, calibrated) | ~$0.01 to $0.04 per artifact (5-call calibration: $0.05 to $0.17) |
| Claudo, one task, before the planner was told the app already exists | **$3.29**, 11 min (two attempts) |

Claudo's per-task rigor is several times the single agent, so it is reserved for MVP and above (`build_from`) and
capped per run (`[engine] budget_usd`). Measure your own: `item.json` records `cost_usd` per item.

## On Windows

Everything runs natively (Python 3.12+, uv, just, Git Bash). `just check` and Claudo's `just gate-ci` are green on
Windows and Linux. See Claudo's `docs/how-to/run-on-windows.md` for the quirks (`python3` Store stub, WSL `bash`,
cp1252, CRLF).

## Layout

```
factory.toml        factory definition (runner, models, gates per maturity, [engine])
radar.toml          company tech radar (IT-owned)
golden_paths/       IT project templates (python-fastapi: app, evals, justfile, CI, Dockerfile)
src/factory/        radar, detect, guard, gates, design compiler, foreman, agents, judge, claudo bridge, cli
work/<slug>/        one folder per work item: item.json + idea/spec/design/tasks/gate-report/judge-*/plan-lint
apps/<slug>/        shipped apps (each its own git repo once built through Claudo)
```
