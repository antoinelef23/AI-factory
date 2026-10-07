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
| ship | The factory stops the orchestrator where it pauses for its human checkpoint, runs its own gates, and parks the item at IT's ship review. IT's approval writes a **signed (HMAC) token** carrying the approver's name; the secret lives in `.factory/` (git-ignored, outside every app). An agent cannot self-approve. |

No Claudo found: the factory still runs, plans are simply not linted and everything builds with the single agent.

## LLM judge (advisory)

`[agent] judge = true` (billed) scores each spec, plan and build against a rubric. It **never blocks**: it tells the
human reviewer where an artifact looks weak. Every score must quote the artifact verbatim (checked by code,
ungrounded scores are discarded), the verdict is computed from the scores, and the judge uses a different, cheaper
model than the generators. `uv run factory judge <slug> --kind spec|plan|build` runs it on demand.

## What it costs (measured, Sonnet generators)

| Path | Result |
|---|---|
| single agent, whole 7-module expense API (POC) | **$0.71**, ~3.5 min, gates green first try |
| plan written by an agent | ~$0.15, passes Claudo's plan-lint first try |
| judge call (haiku, default thinking) | $0.05 to $0.09 per artifact |
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
.factory/           factory secrets (git-ignored)
```
