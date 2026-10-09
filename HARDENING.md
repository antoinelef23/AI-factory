# HARDENING: plan from the 2026-10-09 audit

Source: [docs/audits/2026-10-09-ultracode.md](docs/audits/2026-10-09-ultracode.md). It is a read-only multi-agent
audit of `3cda477`, made from another account: 178 confirmed findings (1 critical, 7 high that reduce to 2 distinct
defects, 59 medium, 111 low). Finding IDs below (`A1`...) refer to its appendix.

This file replaces ROADMAP section 10 as the work order. Feature work (more golden paths, phase 4) waits until
H1 to H4 are done.

## Triage (re-checked on `main` = 4827d6b, 2026-10-09)

| Audit item | Verdict | Evidence |
|---|---|---|
| A1, A8: sandbox escape through `.git` | **Confirmed, critical.** Also present in Claudo. | `sandbox.py:111` mounts the app `:rw`, `.git` included. Then the host runs plain `git` in it (`project.py` `commit_all`, `porcelain`). A planted `.git/hooks/pre-commit` or `core.fsmonitor` runs as the operator, next to the signing secret. Claudo has the same pattern: `sandbox_runner.py:199` mounts `:rw`, and `orchestrate.py:376` commits on the host. |
| A4-A7, A51-A53: the app's `golden.toml` overrides IT's gate commands | **Confirmed, high.** | `foreman.py:1670` `{**cfg.gate_commands, **golden_gate_commands(app)}` reads `app/golden.toml`, which the agent can write. `tomllib` is unguarded (`:1707`). |
| A2, A3: targeted fix never committed, so clean_tree loops | **Already fixed after the audited commit.** | `0d5cd50` (+ `4827d6b` wording) commits the fix apart with a `fixed_after_review` ack. Regression test `test_a_targeted_fix_is_committed_apart_and_shown_to_it`. Verified live on utc-clock (8/8 gates). |
| A45 + trajectory row: `claudo_cp` stands for three facts | **Confirmed.** | `foreman.py:1372` replays all of Claudo when `claudo_cp == ""` after a "done" run. `:1655` skips the trajectory gate in the same case. |
| A46-A48: `--unsafe-host` at approve/run silently moves agent code to the host | **Confirmed.** | `_claudo_env` (`:1454`) and `_gate_executor` (`:1437`) fall back to the host with `build_where` still `"sandbox"`. The ack is only added in `_do_build`. |
| A38-A41: title pasted raw into code | **Confirmed.** | `foreman.py:944` replaces `{{title}}` verbatim in `main.py`, `pyproject.toml`, `App.tsx`, `index.html`. |
| Other mediums and lows | **Accepted as a backlog.** | Each one gets re-checked when its package starts: a failing test first. A finding that does not reproduce is recorded as refuted here, not silently dropped. |

What the audit could not see: nothing was executed. The live sandbox run (PROGRESS, utc-clock) is the only
end-to-end evidence so far, and it would not have exposed H1, because a cooperative agent plants nothing.

## Work packages (in order; one fix per commit, `just check` green between)

### H1. Nothing under `apps/<slug>` is host configuration or host-executed code (critical + high)
1. **Sandbox mounts `.git` read-only.** Factory `Sandbox.argv`: add `-v app/.git:/workspace/.git:ro` (a file or a
   directory). Claudo `build_docker_argv`: the same. Agents never commit, because Claudo's orchestrator commits on
   the host. `lab-ws` only reads git. Test: argv carries the ro mount. A live probe: writing `.git/hooks/x` from
   the container fails.
2. **Host git is hardened.** Every factory `git` call goes through one helper passing `-c core.hooksPath=<empty
   dir> -c core.fsmonitor=false -c commit.gpgsign=false` (this also covers A152, the operator's own `hooksPath`/`gpgsign`) and `GIT_TERMINAL_PROMPT=0`, with timeouts. Claudo's `orchestrate.py` git calls get the same helper. Test: a
   planted `pre-commit` hook and an `fsmonitor` command do not run on `commit_all` / `porcelain`.
3. **The factory refuses a `.git` it did not create.** A non-git app found with a `.git` before `git init`, or a
   `.git/config` whose hash changed since the factory last wrote it: the gate fails, with the reason stated.
4. **Gate commands come from IT's golden path only.** Read them from `golden_paths/<recorded path>/golden.toml`
   (`GoldenPath.gate_commands`, already loaded). Record the path on the item at scaffold. A parse error is a failed
   gate, not a traceback. Test: an agent edits `app/golden.toml` to `tests = "true"`, and the factory command still
   runs.
5. Related trust leaks in the same pass: the judge skips symlinks (A126); `find_root` refuses a root under
   `apps_dir` (A90); history scan `git log -p --text --no-textconv` (A58); the container gets a name and is
   `docker kill`ed on timeout (A65), and Claudo's watchdog kills the process group (A12); credentials go through
   `-e KEY` with the value in the env, never in argv, and gate containers get none.

### H2. The containment record never claims more than what happened (A46-A48, A65)
- Before any agent code runs (build, gates, Claudo's final run), the factory re-decides where it runs. If that is
  the host while `build_where == "sandbox"`, it refuses unless `--unsafe-host` was given, and then it downgrades
  `build_where` to `"host"` and adds the `unsafe_host` ack. `_finalize_with_claudo` checks `sandbox.problems()`.
  Test: approve with `--unsafe-host` after a sandboxed build gives the ack, and the PR body says "host".

### H3. Untrusted text never becomes code (A38-A41, prompt rows)
- Placeholders are escaped per target: `json.dumps` for Python/TS string literals, TOML basic-string escaping,
  `html.escape`. Docstrings and README headings get a sanitized one-line title. Ideas are fenced or quoted in
  prompts. Test: a title with `"`, `\`, `"""` and `</title><script>` produces files that parse, and offline gates
  pass.

### H4. State machine and flags (theme 3 and 4)
1. Split `claudo_cp` into `built_with_claudo`, `claudo_cp` (pending checkpoint) and `claudo_rework_pending`. This
   fixes A45 (retry routing), the trajectory skip, A49 and A50.
2. Terminal states: `abandoned` is refused by run, approve, reject, allow, merge and publish (A27, A28). `abandon` is
   refused when `pr_state == MERGED` (A43, A44). `merge` requires tip == `approved_head` (A42). `promote` refuses
   while a change is open, and resets acks, `claudo_*`, `gated_sha` and `.runs/state.json` (A33-A35). `run()` blocks
   when its step budget runs out (A114, A115).
3. Persistence: `run()`/`approve()` save in try/finally, and engine, timeout and TOML errors become `FactoryError`
   (A30, A36). `item.json` is written atomically, and one bad item does not break `Store.all()` (A174). The human rejection
   reason gets its own field (A29). The inbox catches errors per issue (A26). `porcelain -z` (A62, A63).

### H5. Detectors fail closed (theme 2)
- Secrets: credential regex forms, `sk-proj-`/`sk-svcacct-`, scanning every text file (sniffing NUL bytes instead
  of a suffix allowlist), `errors="replace"`, tracked `vendor/`/`venv/` (A54-A58, A101).
- Radar: requirements includes, `[tool.uv] dev-dependencies`, `optionalDependencies`, and Pipfile, setup.py,
  setup.cfg and environment.yml are parsed or flagged `unanalysed`. Dockerfile stage aliases and `${ARG}` are not
  images. `COPY --from=<image>` is an image. `import a, b` is parsed with `ast` (A20-A24, A98-A100, A102, A103).
- Non-git apps are git-initialised and committed BEFORE gating, so gates and the approval seal see the same tree
  (A19, A31, A32).
- `factory check` on a missing path fails (A17). radar-import handles null/list values, `text_strict` for short
  names, and cp1252 (A59, A60, A87). Keyword triage handles negations, and bare `ai` is dropped (A18).

### H6. Golden paths are production-grade and CI-tested (A9-A11, A74-A81)
- Dockerfiles: `uv sync --locked --no-dev`, run the venv binary, non-root `USER`, pinned base images and uv,
  `.dockerignore`. The worker image ships `worker/` and has a consumer entrypoint. Template CI uses `--locked`.
- Root CI: a job that scaffolds each template, runs its pytest (+ `npm test`/`build`) and `docker build`s it.

### H7. Tests, calibration, docs
- The about-endpoint calibration requires a grounded fidelity/scope score <= 3 (A67). `just calibrate` defaults to
  the production judge config. Regenerate the ping-service baseline (A73). Add a test for the dirty-tree refusal at
  approval (A120).
- Docs: ROADMAP status tables, README drift and migration claims, stale PROGRESS lines (A69-A72).

### Remaining lows
Picked up when their file is touched by a package above. Whatever is left after H7 gets one sweep and a
refuted/fixed/deferred line each.

## Status

- **H1 done (2026-10-09).** Factory `11dbeae` (hardened host git + .git/config allowlist), `7606276`
  (.git read-only in the container), `3fa1f9c` (every app in git before any agent), `d529ed5` (gate commands
  from IT's golden path), `5e24797` (process-tree kill, container reaping, credential by name, judge symlinks,
  apps' factory.toml). Claudo `c843e37` (read-only .git, no hooks in sandbox mode), `a7bd16b` (run label).
  Live probe in the real container (docker only, free): writing `.git/hooks/pre-commit` and
  `git config core.fsmonitor` from the container fails with "Read-only file system"; the host commit that
  follows runs nothing. Control with the pre-H1 mount: both were planted, and plain host git EXECUTED both
  on Windows. The factory's allowlist then refused the planted config.

## Exit criteria
- H1-H4: each fix has a test that fails on the old code, `just check` and Claudo `just gate-ci` are green, and
  CI is green after the push (with Antoine's go).
- H1 live: a hostile-agent probe in the real sandbox (plant a hook, an fsmonitor command and an `app/golden.toml`
  override). Afterwards, the host commit runs nothing and the gates use IT's commands. No model call is needed
  (docker only, free).
- After H1-H3, one billed sandboxed MVP smoke (about $2, Antoine's go) confirms nothing regressed end to end.
