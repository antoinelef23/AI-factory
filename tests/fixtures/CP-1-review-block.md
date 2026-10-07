# Review CP-1 — panel of 1 — aggregated verdict: BLOCK (BLOCK)

## Panelist 1 — claude-opus-4-8 — lens: full review
VERDICT: BLOCK

The earlier `git diff` output already confirms this: the docstrings and the README `## Endpoints` section appear as added (`+`) lines in the working-tree diff, so the committed HEAD lacks both. I have everything I need.

---

# CP-1 Ship Review — ping-service · Task T3 (EVAL-11)

**Lens:** full review · **Merge:** human (blocking) · **Re-review after Owner rejection of 2026-10-07 17:49**

## Verification (run this session, working tree)
- ✅ `uv run pytest -q` → **12 passed**, 1 benign upstream warning (starlette/httpx deprecation)
- ✅ `uv run ruff check .` → **All checks passed!**
- ⚠️ **These greens hold only for the uncommitted working tree.** The committed HEAD does not pass (see Blocker 1).

## 1. Traceability
- ✅ `tests/test_smoke.py:18` implements EVAL-11 (`test_eval_11_…`, `@pytest.mark.eval` at `:17`); marker registered `pyproject.toml:17`.
- ✅ Assertions match spec §7 EVAL-11: 100 sequential requests (`test_smoke.py:22`), all status + exact body (`:29`, `:32`), p95 ≤ 200 ms (`:36-39`).
- ✅ EVAL-11 realized in commit `23e4b51` (subject `[EVAL-11]` + `Spec-IDs: EVAL-11`).
- ⚠️ HEAD `6a83681` reuses the same `feat(…): T3 … [EVAL-11]` subject but its diff is pure bookkeeping (`.runs/`, `.approvals/`, `tasks.md`) — no EVAL-11 code. Two near-duplicate T3 commits; cosmetic, not blocking.

## 2. Design conformance
- ✅ T3 adds only a `TestClient` pytest — within the adopted ring (design §3, ADR-1). No new technology.

## 3. Scope / quality of the T3 deliverable
- ✅ `tests/test_smoke.py` only; **app code unchanged** since T2 — honors T3's "Change no app code".
- ✅ p95 via nearest-rank (`int(100*0.95)-1 = 94`, `test_smoke.py:37`) is a correct approximation for n=100.
- ℹ️ Latency is measured in-process through `TestClient`, not real uvicorn HTTP. This is the explicitly approved T3 task wording ("Using TestClient…"), so it is conformant — noted only so the Owner knows the smoke measures handler+serialization, not network.

**T3 in isolation is clean.** The blockers below are about the *checkpoint state the human is about to merge.*

## ❌ Blocker 1 — Committed state fails its own merge gate (EVAL-6 / INV-1)
- `pyproject.toml` at HEAD still declares `pydantic>=2.7` (`HEAD:pyproject.toml:8`). EVAL-6's allowlist is `{fastapi, uvicorn, httpx, pytest, ruff}` + `pytest-*` (`tests/test_invariants.py:59`). `pydantic` is not on it → `violations == ['pydantic']` → **EVAL-6 fails on the committed tree**.
- It only passes now because the working tree removes that line (`git diff pyproject.toml`, uncommitted). If the human merges the branch as committed, INV-1 is red. "No green gate, no ship" (design §5).

## ❌ Blocker 2 — The Owner's rejection fix is uncommitted
- CP-1 was rejected 17:49 requesting "a docstring to eve