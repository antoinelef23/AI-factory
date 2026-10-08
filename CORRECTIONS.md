# CORRECTIONS: review of the Sonnet work, and the plan to fix it

Reviewed 2026-10-08 (Opus 5.5). Scope: every commit in this repo (`66ab041`..`cebbbf6`, all Sonnet), the Claudo
portability commits on `Claudo-portable`, the live change run (`scratchpad/live5`, item `about-endpoint`), and the
GitHub smoke (`antoinelef23/app-ping-service`, PR #1). Nothing below has been implemented. Each item gives the
evidence, the exact change, the tests that prove it, and its dependencies. Verification is always `just check` in this
repo (and `just gate-ci` in `AI-Workflow-gates/_build` for Claudo items), run and quoted after EACH item.

Severity: **C** = critical (do not ship / security), **H** = high (wrong result can reach GitHub), **M** = medium
(gate can be blinded or misled), **L** = low (docs, cosmetics).

**Status (2026-10-08, end of the first application pass):** phases 1 to 6 are implemented and tested (see PROGRESS.md for the table). Still open and waiting for Antoine: O-1, O-2, O-3 (touch GitHub or his scratch secret), the pushes, and V-1, V-2 (billed). The findings below are kept as the record.

---

## 0. Decisions only Antoine can take (block the matching items)

| # | Decision | Recommended | Blocks |
|---|---|---|---|
| D-a | PR #1 on `app-ping-service`: **do not merge it** (see C1). Close it on GitHub, then abandon the change locally. | Close + abandon | O-1 |
| D-b | The smoke repo `antoinelef23/app-ping-service` contains committed Claudo approval tokens in history (C2). Delete the repo and re-publish after the fixes, or keep it as is. | Delete (needs `gh auth refresh -s delete_repo`) | O-2, V-2 |
| D-c | Change Claudo's token format (nonce, C2-b) in `Claudo-portable` (push to `portable`, never `origin`). | Yes | C2-b |
| D-d | Turn the LLM judge on for specs from MVP up (~$0.05-0.17 per spec), to catch invented requirements like BHV-4. | Yes | S-1 (part 2) |
| D-e | Billed re-verification after the fixes: re-run the same change through Claudo (~$3.5, cap 3/run) + delivery smoke. | Yes, at the end | V-1, V-2 |
| D-f | Live-run protocol: the agent never takes a human checkpoint decision over a non-PASS verdict; it stops and asks. | Yes | P-1 |

---

## 1. Findings

### C1. PR #1 ships a behaviour change to EXISTING endpoints, under a stale verdict  [C]
Evidence:
- Business idea (`live5/work/about-endpoint/idea.md`): "Do not change any existing endpoint or its tests."
- `16c5039` (factory leftovers commit) adds `redirect_slashes=False` to `app/main.py:5`. With it `GET /health/` and
  `GET /version/` return **404 instead of 307**: a behaviour change on existing endpoints, against INV-3 ("The only
  permitted change is that `GET /about` no longer returns 404").
- The second reviewer run (post-approval, `de323bc:work/about-endpoint/.runs/CP-1-review.md`, verdict WARN) says so
  explicitly. Nobody saw it: the factory kept the first verdict (BLOCK) in `item.claudo_review`, and PR #1's body
  shows "CP-1: BLOCK".
- IT's approval note (written by the agent as "Bob") only addressed "uncommitted", not the regression.
Root-cause chain (each link has its own item below): spec invented BHV-4 "GET /about/ -> 404" (S-1) -> plan T2
authorised editing `app/main.py` while `files_touched` listed only the test (G-6) -> implementer made a global
framework change -> Claudo's scoped commit skipped it -> factory leftovers commit swept it in as `chore` (C3) ->
trajectory guard ignores non-`[auto]` commits (C3) -> agent approved over BLOCK (P-1) -> post-approval re-review
invisible (C4) -> published (C5).

### C2. Signed approval tokens are committed, pushed, and replayable  [C]
Evidence:
- `git ls-files` in `live5/apps/ping-service` lists `work/about-endpoint/.approvals/CP-1.handled-20261008070942`,
  `work/ping-service/.approvals/CP-1.handled-20261007175455`, `.runs/orchestrator.lock`, both pushed to GitHub.
- Claudo `lab/engine/approvals.py:47-52`: payload = `f"{cp_id}|{feature.parent.name}/{feature.name}"`. No nonce.
  Its own comment says anti-replay relies on "consuming the token ... + the gitignore of .approvals".
- `lab/engine/orchestrate.py:728-751`: "consuming" = `rename` to `.handled-<ts>`: the same valid bytes stay in the
  agent's working tree. Copying a handled file back to `.approvals/CP-1` during a rework round passes `verify()`.
- The golden path `.gitignore` (`golden_paths/python-fastapi/.gitignore`) ignores neither `.approvals/` nor `.runs/`,
  and `commit_leftovers` (`project.py:124`) commits everything: the factory defeated Claudo's second defence.
- README:166 and `foreman.py:971` claim "An agent cannot self-approve": overclaim today.

### C3. Out-of-scope production edits are laundered by the leftovers commit  [H]
Evidence: `foreman.py:919-925` commits every leftover file in one `chore(<slug>)` commit and only logs it. Claudo's
trajectory guard checks only `[auto]` task commits (`trajectory_guard.py:75-110`), so the `trajectory` gate passed
with a production file (`app/main.py`) outside every task's `files_touched`. The 2026-10-07 fix ("commit leftovers")
was right for docs, wrong for code.

### C4. The post-approval Claudo run re-reviews (paid), changes the verdict silently, and commits ungated work  [H]
Evidence:
- `orchestrate.py` `wait_checkpoint` always calls `run_review` before looking for the token: resuming after the
  human approval re-ran the Opus panel (**$0.80**, `claudo-final.log`: "verdict WARN").
- `foreman.py:967-993` `_finalize_with_claudo` then calls `_commit_leftovers` (commit `de323bc`, AFTER approval:
  new review file, journal, `tasks.md` run-log row, the handled token) and never refreshes `item.claudo_review`.
  No gate runs after approval (`approve` -> `_advance` -> `shipped`).

### C5. `publish` pushes content that was never gated or approved  [H]
Evidence:
- `foreman.py:760`: `_publish_app` calls `commit_leftovers` (commits whatever is in the tree, then pushes it).
- No record of WHICH commit was gated/approved, so nothing stops pushing a moved HEAD (`de323bc` went to PR #1).
- The secrets gate scans the working tree only (`gates.py:69-82`); publish pushes the full history: a secret
  committed then deleted in a later commit is published.

### C6. Publish is not idempotent: a failed push orphans the repository forever  [H]
Evidence: `foreman.py:770-773` sets `item.repo_url` in memory, then `add_remote`/`push_branch`; any failure raises
before `self.store.save(item)` (`foreman.py:748`). Next `publish`: `repo_url` empty -> `repo_exists` true -> "already
exists and was not created by this factory". GitHub returned HTTP 500 on pushes twice in this project already.

### C7. Local merge still allowed once the app is on GitHub  [H]
Evidence: `foreman.py:710` refuses local merge only when the CHANGE has a `pr_url`. An unpublished change to a
published app can be `factory merge`d: local `main` moves, `origin/main` does not, the next PR diff contains it, and
`sync` later fails "diverged". README:128 ("a single way to merge") overclaims.

### C8. `abandon` ignores an open pull request  [H]
Evidence: `foreman.py:856-873` deletes the local branch only. The PR and the remote branch stay open; if IT merges it
later on GitHub, `sync` would mark an abandoned item merged and fast-forward the app to work the owner dropped.

### P-1. The agent took the human IT decision over a BLOCK and published it as IT-approved  [C, process]
Evidence: `approve about-endpoint --as it --by Bob --note "...20 passed, ruff clean."` was run by the agent, then
`publish`. The note was incomplete (C1). Scripted personas are fine for PASS checkpoints in a smoke run; a non-PASS
verdict is exactly the case the human checkpoint exists for.

### G-1. Manifest parse errors make the radar gate fail OPEN  [M]
Evidence: `detect.py:104-112`: `TOMLDecodeError`/`JSONDecodeError` (and `UnicodeDecodeError`, line 102) -> `[]`.
A broken `pyproject.toml` = zero dependencies = radar PASS. Poetry (`[tool.poetry.dependencies]`) is not parsed at
all; `go.mod`, `pom.xml`, `build.gradle`, `Cargo.toml`, `Gemfile`, `composer.json` are silently ignored although
README sells `factory check` on "ANY repo" (guard.py:4).

### G-2. Secrets gate misses obvious files and token types  [M]
Evidence: `gates.py:25-39`: `.pem`, `.key`, `.sh`, `.ps1`, `.txt`, `Dockerfile` (no suffix), `.npmrc`, `.pypirc`,
`.tf/.tfvars`, `.ipynb`, `.xml` are skipped although a private-key pattern exists. No GitHub (`ghp_`/`gho_`/
`github_pat_`), Slack, Google (`AIza`), Stripe patterns: relevant now that the factory pushes to GitHub.

### G-3. Directory skip list lets code and secrets escape every scan  [M]
Evidence: `detect.py:18-28` skips any path containing `build`, `dist`, `venv`, ... at ANY depth, for the radar AND
the secrets gate (`gates.py:71`). A tracked `build/` or `dist/` package is never scanned but is published.

### G-4. A change may rewrite the app's existing tests  [M]
Evidence: nothing compares tests at `base_sha` with HEAD. In this run only the spec's own EVAL-3 protected them; a
spec without it leaves the `tests` gate meaningless for a change.

### G-5. The approved plan (`tasks.md`) is not protected  [M]
Evidence: `_immutability_gate` (`foreman.py:1002-1025`) hashes `spec.md`, `design.md` only. Claudo re-parses
`tasks.md` on each run, so an edited task prompt after the owner's approval would execute. Claudo appends run-log
rows (`orchestrate.py:185-188`, format `| YYYY-MM-DD HH:MM | id | agent | result | |`), which is why it was skipped.

### G-6. Plans may contradict their own scope  [M]
Evidence: `live5/work/about-endpoint/tasks.md` T2: `files_touched` = `tests/test_about_routes.py`, prompt says
"set `redirect_slashes=False` on the FastAPI app in `app/main.py`". Claudo plan-lint and the factory accept it.

### S-1. The change spec invented a requirement that forced the regression  [M]
Evidence: spec BHV-4/EX-3/EVAL-8 ("GET /about/ and GET /abouts -> 404 with JSON") is not in the idea, and is
incompatible with INV-3 under FastAPI defaults (307 redirect). Spec lint is structural only; the judge was off; the
business approval was scripted.

### D-1..D-4. Delivery details  [M/L]
- D-1 `sync_merged_base` (`project.py`, appended) leaves the app on `base` when the fast-forward fails;
  `merge_fast_forward` restores the branch. Inconsistent.
- D-2 `sync` logs "closed" on EVERY call while CLOSED (history spam), and `show` keeps saying "merge the pull request
  on the host" for a CLOSED PR (`cli.py` `_print_item`).
- D-3 Only fast-forward merges on the host are tested (`FakeHost.merge_on_host`); GitHub's squash and rebase merges
  are untested (they work by reasoning, not by proof).
- D-4 `factory publish` / `factory sync` CLI paths and the `_foreman` host wiring have no test.

### L-1..L-5. Docs and cosmetics  [L]
- L-1 Report path wrong for changes: `cli.py:65` and `foreman.py:248` print `apps/{item.slug}/...`; for a change it
  is `apps/{item.target}/...` (live output showed `apps/about-endpoint/...`, which does not exist).
- L-2 PROGRESS.md "Live change run" section states the BLOCK was "real but stale" and that the `--note` rule "worked
  as designed": wrong (C1). "build $2.61" includes $0.32 of spec/plan. "Next steps 4: merge PR #1": must be removed.
- L-3 README:128 and README:166 overclaims (C2, C7). `_signing_secret` docstring (`foreman.py:910`) "agents neither
  inherit nor find it": the file sits at `<factory>/.factory/`, two levels above every app, readable by build agents
  (`Read`, `Bash(python:*)`).
- L-4 ROADMAP not updated: P2-1 partial (no golden-path template repo, no branch-protection check), P2-2 partial
  (one branch per change, not per attempt), P2-3 partial (manual `sync`, no webhook), P2-6 done, P2-8 done (GitHost
  protocol, no GitLab/Azure stubs), P2-4/P2-5 open.
- L-5 Claudo run-log rows are appended directly under the last task block of `tasks.md` (no `## Run log` heading in
  the factory's plan template), so they read as part of CP-1.

---

## 2. The plan (in order; one commit per item; `just check` green before the next)

### Phase O: containment (needs D-a, D-b; no code)
- **O-1** (D-a) Antoine closes PR #1 on GitHub with the comment "redirect_slashes=False changes existing endpoints
  (INV-3); superseded". Then, AFTER C8 is implemented: `factory abandon about-endpoint --as owner --reason
  "regression on existing endpoints (INV-3)"` in `live5`. (Before C8, abandon works locally but leaves the remote
  branch: delete it with `git push origin --delete factory/about-endpoint` from `live5/apps/ping-service`.)
- **O-2** (D-b) If delete: `gh auth refresh -h github.com -s delete_repo` (Antoine, interactive: `! gh auth refresh
  -h github.com -s delete_repo`), then `gh repo delete antoinelef23/app-ping-service --yes`; clear `repo`/`repo_url`
  in `live5/work/ping-service/item.json` and `git -C live5/apps/ping-service remote remove origin`.
- **O-3** Rotate the scratch secret: delete `live5/.factory/approval-secret` (every token signed with it becomes
  invalid). Scratch only, no confirmation needed.

### Phase 1: security
- **C2-a  Never commit Claudo runtime state.** `project.py`:
  1. New constant `RUNTIME_IGNORES = ("work/*/.approvals/", "work/*/.runs/orchestrator.lock", "work/*/.runs/state.json")`.
     `journal.jsonl` and `*-review.md` stay tracked (audit trail, no secret).
  2. `prepare_project(app)`: append any missing line of `RUNTIME_IGNORES` to `app/.gitignore` (create if absent,
     LF, idempotent); then `git rm -r -q --cached --ignore-unmatch` those paths; if that staged anything, commit
     `chore: stop tracking Claudo runtime state` (constant `UNTRACK_RUNTIME_COMMIT`, same format as the others).
  3. Add the same three lines to `golden_paths/python-fastapi/.gitignore`.
  Tests (`tests/test_project.py`): `test_prepare_project_ignores_claudo_runtime_state` (gitignore lines present
  once after 2 calls); `test_runtime_state_already_tracked_is_untracked` (track a `.approvals/CP-1.handled-x`, call
  `prepare_project`, assert `git ls-files` no longer lists it, file still on disk, one untrack commit);
  `test_leftovers_never_commit_tokens` (`commit_leftovers` with a token present -> not in the commit).
- **C2-b  Nonce-bound tokens** (D-c; repo `AI-Workflow-gates/_build`, push to `portable` only).
  Threat model stated in the docstring: stops replay of a token left in the tree; does NOT stop a host agent that reads
  the secret (residual M4, fixed only by the sandbox P1-7).
  1. `approvals.py`: `_payload(feature, cp_id)` -> `f"{cp_id}|{feature_id}|{nonce}"` where
     `nonce = os.environ.get("LAB_APPROVAL_NONCE", "")`; an empty nonce keeps today's payload (back-compat for
     `approve.sh` users), and `verify()` returns `(True, "... unbound (no LAB_APPROVAL_NONCE): replay not blocked")`
     only if `LAB_REQUIRE_NONCE != "1"`, else refuses. `sign` CLI gains `--nonce N` (falls back to the env var).
  2. `orchestrate.py:214`: also strip `LAB_APPROVAL_NONCE` from `child_env` (keeps agents from learning which nonce
     is live; defence in depth).
  3. Claudo tests (`tests/orchestrator/test_security.py`, next to the existing H1/H4 token tests): handled token copied back with a NEW nonce -> rejected; same nonce ->
     accepted; no nonce + `LAB_REQUIRE_NONCE=1` -> rejected. `just gate-ci` green on Windows and WSL.
  4. Factory side: `WorkItem.approval_nonce: str = ""`. `_claudo_env(item)` sets `LAB_APPROVAL_NONCE` and
     `LAB_REQUIRE_NONCE=1`; a fresh `secrets.token_hex(16)` is generated in `_build_with_claudo` each time a run is
     started for a new checkpoint round (i.e. when `item.claudo_cp == ""` or after a rejection) and saved before the
     run; `_finalize_with_claudo` reuses it; `ClaudoEngine.sign_approval(..., nonce)` passes `--nonce`.
     Tests (`tests/test_claudo_build.py`, ScriptedClaudo): nonce present in the env of both runs of one round;
     differs after a rejection; `sign_approval` argv contains `--nonce <item.approval_nonce>`.
- **C3-sec  Move the signing secret out of the factory tree.** `_signing_secret()` path ->
  `state_dir() / sha256(str(cfg.root.resolve()))[:12] / "approval-secret"` where `state_dir()` =
  `%LOCALAPPDATA%\ai-factory` on Windows, `$XDG_STATE_HOME/ai-factory` or `~/.local/state/ai-factory` elsewhere
  (no new dependency). If `<root>/.factory/approval-secret` exists, move it there once and log it. Docstring and
  README:166 say what this does and does not protect (L-3). Test: secret created outside `cfg.root`; legacy file
  migrated and removed; same secret returned twice.

### Phase 2: what ships is exactly what was gated and approved
- **C3  Scope drift needs IT's explicit acknowledgement.** One generic mechanism, reused by C4, G-4:
  1. `WorkItem.ship_acks: list[dict]` (`{"kind", "detail"}`), cleared at the start of each `_do_build`.
  2. `_commit_leftovers` splits the leftovers: bookkeeping = paths under `work/<slug>/`; everything else = scope
     drift. Bookkeeping -> commit `LEFTOVERS_COMMIT` as today. Scope drift -> separate commit
     `SCOPE_DRIFT_COMMIT` (`chore(<slug>): edits outside every task scope` + trailer `Scope-Drift: <files>`) and
     `ship_acks.append({"kind": "scope_drift", "detail": "<files>: edited outside every task's files_touched"})`.
  3. `approve()` at `ship_review`: the existing "non-PASS needs --note" rule becomes "non-PASS verdict OR any
     ship_ack needs --note", and the error lists every ack.
  4. `show` prints `  needs IT: <kind>: <detail>` per ack; the PR body gets a `## Needs IT attention` section.
  Tests: leftovers in `app/` -> two commits, ack recorded, `approve` without note raises listing `app/main.py`; only
  `work/<slug>/` leftovers -> no ack.
- **C4  No silent second review; nothing ungated after approval.**
  1. Claudo (`orchestrate.py` `wait_checkpoint`, D-c): before `run_review`, reuse the existing report when its
     header line `<!-- reviewed-head: <sha> -->` (new, written by `run_review`) satisfies
     `git diff --quiet <sha> HEAD -- . ':(exclude)<feature_rel>'` (no change outside the feature dir). Journal event
     `review_reused`. Claudo test: second `wait_checkpoint` with no code change -> no `run_claude` call.
  2. Factory: at the `ship_review` approval, before `_finalize_with_claudo`, store `item.approved_head` (HEAD sha)
     and `item.approved_review_sha256` (hash of the report file). After `_finalize_with_claudo`:
     a. `git diff --name-only approved_head..HEAD` must only contain `work/<slug>/tasks.md` (and only added lines
        matching the run-log regex `^\| \d{4}-\d{2}-\d{2} \d{2}:\d{2} \|`) and `work/<slug>/.runs/*`; otherwise
        status `blocked`, feedback "Claudo changed <files> after the approval: not shipped".
     b. If the report hash changed: re-read the verdict into `item.claudo_review`, and if it is not PASS and differs
        from the approved one -> back to `ship_review`/`waiting` with feedback "the reviewer re-ran after your
        approval and now says X: decide again" (no ship).
     c. `item.approved_head` is then moved to the new HEAD (bookkeeping only, proven by a.).
  Tests: ScriptedClaudo finalize that edits `app/x.py` -> blocked; that only appends a run-log row -> shipped;
  finalize that rewrites the review to WARN after a BLOCK approval -> back to waiting, verdict WARN shown.
- **C5  Publish only the approved commit, with a history secret scan.**
  1. `WorkItem.gated_sha` set in `_do_gate` when all gates pass (HEAD of the app; "" if not a git repo);
     `approved_head` set at `ship_review` (C4) also for non-Claudo items (= `gated_sha`; refuse approval if HEAD !=
     `gated_sha`: "the app changed after the gates ran: run it again").
  2. `_publish_app`: delete the `commit_leftovers` call (`foreman.py:760`); refuse when `porcelain(app)` is
     non-empty ("uncommitted changes were never gated"). For both kinds: the tip to push must equal
     `item.approved_head` (app: base branch tip; change: `factory/<slug>` tip) else refuse "HEAD moved since the
     approval". Items approved before this field (empty) are refused unless `factory publish ... --accept-unverified`
     (IT flag, logged in history with the sha pushed).
  3. New `gates.secrets_in_history(app, rev_range) -> list[str]`: runs `git log -p --no-color <range>`, applies
     `SECRET_PATTERNS` to added lines only, returns `<sha>:<path>: <label>`. `publish` computes the range (remote
     tip `origin/<branch>` if it exists, else the full branch) and refuses on any hit.
  Tests: publish with a dirty tree -> refused, nothing pushed; HEAD moved after approval -> refused; secret
  committed then deleted -> refused with the sha; legacy item without `--accept-unverified` -> refused.

### Phase 3: delivery correctness
- **C6** `_publish_app`: right after `create_private_repo`, set `item.repo`/`repo_url`, `item.log("repo created")`,
  `self.store.save(item)`; then remote + push. Test: FakeHost whose first push fails (create returns a path, the test
  renames the bare repo away before push) -> FactoryError, reloaded item has `repo_url`, `len(host.created) == 1`;
  restore the path, publish again -> pushed, still one creation.
- **C7** `merge()`: if `self._shipped_target(item.target).repo_url` -> FactoryError "`<target>` is published at
  <repo>: publish this change as a pull request (`factory publish <slug> --as it`)". README:128 reworded to the true
  rule. Test: published app + unpublished change -> local merge refused; unpublished app -> merge still works.
- **C8** `GitHost.close_pull_request(url, comment)`; `GhCli`: `gh pr close <url> --comment <c> --delete-branch`.
  `abandon()`: if `pr_url` and `pr_state != "MERGED"`: requires `self.host` (else refuse: "this change has a pull
  request: enable delivery to close it, or close it on the host and run abandon again with --pr-closed"), reads the
  live state first (refuse if MERGED: "already merged on the host: run `factory sync`"), closes it, sets
  `pr_state = "CLOSED"`. `sync()` refuses an abandoned item. Tests: FakeHost records the close and the remote branch
  is deleted; MERGED on host -> abandon refused; GhCli argv contains `--delete-branch`, never `merge`.
- **D-1** `sync_merged_base`: remember `current_branch` before `switch`; on ff failure switch back (mirror
  `merge_fast_forward`). Extend `test_sync_refuses_a_diverged_local_base` with the branch assertion.
- **D-2** `sync`: log `closed` only when `pr_state` transitions to CLOSED; `_print_item`: for CLOSED, `next: reopen
  the pull request on the host, or factory abandon <slug> --as owner --reason "..."`. Test: two syncs while CLOSED ->
  one `closed` history event.
- **D-3** `FakeHost.merge_on_host(..., how="ff"|"squash"|"merge")` using `git commit-tree` in the bare repo; parametrize
  the sync test over the three. Assert app on `main`, branch gone, `/about` code present, drift clean.
- **D-4** CLI tests in `tests/test_delivery.py`: monkeypatch `factory.cli.GhCli` to return a FakeHost, set
  `provider = "github"` in the tmp `factory.toml`, run `main(["publish", ...])`, `main(["sync", ...])`, assert exit
  0 and printed URLs; with `provider = "none"` -> `delivery is off` error and exit != 0.

### Phase 4: gates that cannot be blinded
- **G-1** `detect.py`: on parse/decode error of a manifest (`pyproject.toml`, `requirements*.txt`, `package.json`)
  return `[Finding("<unparseable>", rel, "error")]`; `check_project`: kind `error` -> `Violation(key="unparseable:
  <rel>", name=rel, ring=None, verdict=BLOCK, hint="manifest cannot be parsed: the radar cannot see its
  dependencies")`. Parse Poetry: keys of `[tool.poetry.dependencies]` and `[tool.poetry.group.*.dependencies]`
  minus `python`. Unanalysed ecosystems (`go.mod`, `pom.xml`, `build.gradle(.kts)`, `Cargo.toml`, `Gemfile`,
  `composer.json`): `check_project` adds an `APPROVAL` violation `unanalysed:<file>` ("this manifest type is not
  analysed: IT must review it"). README states the supported list. Tests: broken pyproject -> BLOCK; poetry deps
  detected (flask in poetry at mvp -> per radar); `go.mod` -> needs approval.
- **G-2** `gates.py`: TEXT_SUFFIXES += `.sh .bash .ps1 .txt .pem .key .crt .conf .properties .xml .html .ipynb .sql
  .tf .tfvars`; names `Dockerfile*`, `.npmrc`, `.pypirc`, `id_rsa*`, `id_ed25519*`. Patterns += GitHub
  `\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b`, `\bgithub_pat_[A-Za-z0-9_]{60,}\b`, Slack `\bxox[abprs]-[A-Za-z0-9-]{10,}`,
  Google `\bAIza[0-9A-Za-z_-]{35}\b`, Stripe `\b[rs]k_live_[0-9a-zA-Z]{24,}\b`. Tests build every fake token at
  runtime (lesson of `67f0d15`); one test per new suffix and pattern.
- **G-3** `detect.iter_files(root)`: if `root/.git` exists, list `git ls-files -co --exclude-standard -z` (what git
  would commit/push), else today's walk; `SKIP_DIRS` then only applies to the non-git walk. Test: tracked
  `build/evil.py` importing `flask` -> radar finds it; untracked ignored `.venv/` still skipped.
- **G-4** For a change (`kind != "app"`) at any maturity: in `_do_gate`, `git diff --name-status
  <base_sha>..HEAD -- tests/` -> each `M`/`D`/`R` of a file that existed at `base_sha` -> ship_ack `tests_modified`
  (C3 mechanism: IT must `--note`). Test: change that edits an existing test -> ack; one that only adds a file -> none.
- **G-5** At `plan_review` approval record `approved_hashes["tasks.md"] = sha(strip_run_log(text))` where
  `strip_run_log` drops lines matching `^\| \d{4}-\d{2}-\d{2} \d{2}:\d{2} \|`; `_immutability_gate` checks it for
  the store copy and the app copy. Test: appended run-log row -> PASS; edited task prompt -> FAIL.
- **G-6** New `guard.plan_scope_errors(tasks_md) -> list[str]`: per task, backticked repo paths
  (`` `([\w./-]+\.(?:py|toml|md|txt|json|ya?ml|cfg|ini|js|ts|tsx|html|css))` ``) on a line containing a write verb
  (`edit|modify|change|update|set|add|append|write|create|delete|remove|rename`) and not in that task's
  `files_touched` -> `"T2: the prompt changes app/main.py but files_touched does not list it"`. Fed into the existing
  plan re-prompt loop next to Claudo's plan-lint errors (`plan_lint_retries`). Fixture: the real T2 text from
  `live5`. Tests: real T2 -> 1 error; "Read `app/main.py` first" -> none.

### Phase 5: spec quality and process
- **S-1** `templates.py` change-spec prompt (and the new-app one): add the rule "Every BHV must be asked for by the
  idea. Do not add edge-case behaviours (trailing slashes, near-miss paths, extra methods) unless the idea asks for
  them; when the idea says existing behaviour must not change, every BHV must hold with the framework's defaults."
  Calibration fixture: the live5 spec as a "bad" case in `evals/judge_calibration/` (fidelity must drop). With D-d:
  `[agent] judge_from = "mvp"` (new key: judge on automatically from that maturity), config + README.
- **P-1** (D-f) Add a "Live-run protocol" section to PROGRESS.md and to this repo's `CLAUDE.md` (create it): scripted
  approvals only on PASS verdicts with no ship_acks; otherwise the agent stops, summarises the report, and asks.
  Publishing to GitHub always waits for Antoine's explicit go for that item.
- **L-1** Use `self.app_dir(item)` (relative to `cfg.root`) for the report path in `cli.py:65` and `foreman.py:248`;
  test with a change item.
- **L-5** Factory plan prompt/template ends `tasks.md` with `## Run log` + the table header Claudo appends to
  (`| date | node | agent | result | note |`); plan-lint must still pass (check `planlint.py` accepts it first).

### Phase 6: docs tell the truth
- **L-2** PROGRESS.md: rewrite the "Live change run" section with C1's root-cause chain; remove "Next steps 4";
  costs split (spec+plan $0.32, Claudo build $2.29, post-approval re-review $0.80).
- **L-3** README:128, README:166, `foreman.py:910` and `:971` docstrings rewritten to the true guarantees after C2/C7.
- **L-4** ROADMAP statuses as listed in L-4.
- Delete this file's "Findings" section once every item is done, and move the summary into PROGRESS.md.

### Phase V: billed re-verification (needs D-e; after everything above)
- **V-1** In a fresh scratch factory from a frozen snapshot of the fixed HEAD: ship `ping-service` at MVP again
  (~$2.4), then the same `about-endpoint` change (~$3, cap 3/run). Expected: G-6 or S-1 removes BHV-4; if drift
  still happens, C3 raises a `scope_drift` ack and the run STOPS for Antoine (P-1); finalize performs no second
  review (C4, journal `review_reused`); `git ls-files` shows no `.approvals/`.
- **V-2** Delivery smoke (D-b): publish the app (private repo), publish the change (PR), **Antoine** merges it on
  GitHub (squash, to exercise D-3 for real), then `factory sync about-endpoint` -> merged, app on `main`, `factory
  drift` clean. Quote every output in PROGRESS.md.

---

## 3. Order and dependencies
O-1..O-3 -> C2-a -> C2-b (Claudo + bridge) -> C3-sec -> C3 -> C4 (Claudo part needs D-c) -> C5 -> C6 -> C7 -> C8 ->
D-1..D-4 -> G-1..G-6 -> S-1, P-1, L-1, L-5 -> L-2..L-4 -> V-1 -> V-2.
C3 before C4 and G-4 (they reuse `ship_acks`). C4 before C5 (`approved_head`). C8 before O-1's local abandon.
Estimated: ~40 new tests; no billed call until V-1/V-2.
