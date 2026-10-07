# PROGRESS

## State (2026-10-07)

The MVP of the AI software factory (Plan A in PLANS.md) is built and verified. It is native Python with no
runtime dependencies, and runs on Windows.

**Verification command:** `just check` (ruff check + ruff format --check + pytest).
Last run: all checks passed, 19 files formatted, **39 passed**.

### Verified in this session
- `just check` green.
- Offline end-to-end demo, run from PowerShell in a scratch copy:
  intake -> spec_review -> plan_review -> build -> gates (real `uv run pytest` on the generated app: 1 passed)
  -> ship_review -> shipped. The business asked for MongoDB (hold); the factory noted it and compiled PostgreSQL.
- Claude runner smoke test: native `claude.exe` resolved behind the npm shim, prompt passed via stdin,
  `haiku`, 1 turn, ok, $0.02.

### Not verified yet
- A full `--runner claude` item (spec + plan + build). Wired and unit-tested with a fake runner, but not
  run for real (billed). That is tonight's test.
- `factory export` into Claudo, then Claudo's orchestrator. Claudo needs Linux/WSL (`fcntl`), and the offline
  tasks.md is not checked against Claudo's plan-lint.

## Open risks / known limits
- Roles are self-declared (`--as it`), so there is no authentication. Next: Claudo's HMAC-signed approvals.
- Capability detection (frontend/database/ai/messaging) is keyword-based on the idea text.
- Text scanning of design.md can false-positive on common words matching radar aliases (e.g. "react").
  Only the deterministic design.md is text-scanned; code and manifests are the real enforcement.
- One golden path only (python-fastapi). A React frontend is chosen in design, but there is no golden path
  or offline scaffold for it.
- On a gate failure, the offline build cannot fix anything (scaffold only): the item stays blocked until a human or
  `--runner claude` fixes it.
- In a fresh PowerShell, `uv`/`just` are on PATH only after reopening the terminal (installed via winget today).
- Unrelated: Claudo's `just gate-ci` is red on native Windows (`fcntl` import in orchestrate.py).

## Next step
Full development plan: [ROADMAP.md](ROADMAP.md) (phases 0-8, exit criteria, decisions D1-D7).
1. Antoine runs README "Test it tonight" sections 1 and 2, and notes friction (ROADMAP P0-1).
2. Then follow ROADMAP section 10 "Next 10 tasks", starting with the license decision (D1) and the first commit.
