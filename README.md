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

## Layout

```
factory.toml        factory definition (runner, models, gates per maturity)
radar.toml          company tech radar (IT-owned)
golden_paths/       IT project templates (python-fastapi)
src/factory/        radar, detect, guard, gates, design compiler, foreman, agents, cli
work/<slug>/        one folder per work item: item.json + idea/spec/design/tasks/gate-report
apps/<slug>/        shipped apps
```
