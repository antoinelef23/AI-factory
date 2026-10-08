# CLAUDE.md: AI-factory

Governed idea-to-app pipeline (spec, design from the tech radar, plan, build through Claudo, gates, human checkpoints).
Read `PROGRESS.md` first: it holds the last verified state and the next step. The correction plan from the
2026-10-08 review is `CORRECTIONS.md`.

## Verification

`just check` (ruff check, ruff format --check, pytest; offline, free, a few minutes). Done means it ran and passed
in this session. Judge it by pytest's OWN exit code: never `pytest | tail` in a chain that commits (a pipe hid four
red tests once; that commit was squashed with its fix before the push); redirect to a file and test `$?`, or use `set -o pipefail`. Write code through files
or the editor, not through shell heredocs: they collapsed `\n` and turned `\b` into backspace bytes here.
Billed tests (`-m live`, `just calibrate`) never run without Antoine's go.

## Live-run protocol (billed runs, scripted approvals, publishing)

Born from the `about-endpoint` run, where a scripted approval went over a reviewer BLOCK and the result was
published (see CORRECTIONS.md C1, P-1).

1. **Scripted personas only approve clean checkpoints.** A script may approve spec, design and plan reviews, and a
   ship review whose Claudo verdict is PASS with no `needs IT:` line in `factory show`. On anything else (a
   non-PASS verdict, a `needs IT:` acknowledgement such as `scope_drift` or `tests_modified`, a blocked item) the
   agent STOPS, summarises the reviewer report and the diff in plain words, and asks Antoine. It never writes the
   `--note` itself.
2. **Read the report, not just the verdict.** Before any ship approval, read the Claudo review report and the diff
   of the branch against its base, and say what was found.
3. **Publishing waits for an explicit go for that item.** `factory publish` creates repositories and opens pull
   requests: ask first, every item. Repositories are always private. The factory never merges; Antoine does.
4. **Smoke first, then the full run**, with the spend cap in `factory.toml` (`budget_usd`). Report the cost.
5. **Never push to Claudo's `origin`** (`antoinelef23/AI-Workflow-gates`). The portable fork is the `portable`
   remote of `AI-Workflow-gates/_build`, and pushing there also waits for Antoine's go.
6. Never delete a repository or a pull request without being asked; it also needs the `delete_repo` scope.

## Conventions

- Python 3.12+, `uv`, `ruff` (line length 110). Tests build token-shaped strings at runtime (no literal tokens).
- One fix or feature at a time, one commit each, `just check` green before the next. Unrelated problems go in
  `PROGRESS.md`, not in passing.
- Never weaken a test or a gate to make it pass without saying so explicitly.
