"""The foreman: moves work items through the stages, stops at human checkpoints.

Automatic stages run back to back; a checkpoint parks the item (status `waiting`) until
the right role approves or rejects it. A failed stage parks it as `blocked` with the
reason in `feedback`, which the next attempt receives.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import shutil
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

from factory.agents import BUILD_TOOLS, READ_ONLY_TOOLS, ClaudeRunner, strip_fences
from factory.claudo import (
    ClaudoEngine,
    EngineError,
    LintResult,
    load_or_create_secret,
    migrate_legacy_secret,
    secret_path,
)
from factory.config import Config
from factory.delivery import DeliveryError, GitHost
from factory.design import (
    CHANGE_KINDS,
    OPTIONAL_CAPABILITIES,
    capabilities_prompt,
    choose_stack,
    detect_capabilities,
    existing_stack,
    grounded_capabilities,
    load_golden_paths,
    pick_golden_path,
    render_change_design,
    render_design,
)
from factory.detect import pyproject_dependencies
from factory.drift import Drift, migration_idea
from factory.gates import Executor, GateResult, format_report, run_gates, secrets_in_history, shell_executor
from factory.guard import check_project, plan_radar_errors, plan_scope_errors
from factory.identity import IdentityError, IdentityProvider, RoleMap
from factory.judge import judge_panel
from factory.project import (
    CHANGE_BUILD_COMMIT,
    CHANGE_TRIPLET_COMMIT,
    ProjectError,
    abandon_change,
    add_remote,
    begin_change,
    commit_all,
    commit_leftovers,
    commit_leftovers_split,
    commit_paths,
    current_branch,
    head_sha,
    lock_dependencies,
    merge_fast_forward,
    merge_plan_copy,
    modified_tests,
    plan_part,
    porcelain,
    post_approval_changes,
    prepare_project,
    push_branch,
    ref_exists,
    rev_parse,
    sync_merged_base,
)
from factory.radar import BLOCK, MATURITIES, Radar, verdict
from factory.sandbox import Sandbox, SandboxedRunner
from factory.speclint import lint_spec
from factory.templates import (
    SPEC_ID,
    build_prompt,
    change_build_prompt,
    change_spec_prompt,
    idea_md,
    offline_change_spec,
    offline_change_tasks,
    offline_spec,
    offline_tasks,
    plan_prompt,
    spec_prompt,
    with_run_log,
)
from factory.workitem import ROLES, STEP_BY_NAME, STEPS, Store, WorkItem

APP_BUILD_COMMIT = """feat({slug}): the app built by the build agent

Why: a single build agent does not commit; the factory records its work as one commit so that the gates,
IT's approval and the publish all judge the same commit.

Artifacts: {files}
Run: auto
"""

FIX_AFTER_REVIEW_COMMIT = """fix({slug}): targeted fix after a failed factory gate

Why: Claudo built the plan and its reviewer ran, then a factory gate failed; one agent fixed what the gate
reported. Committed apart so IT sees exactly what changed after Claudo's review, which did not see it.

Artifacts: {files}
Run: auto
"""

RESTORE_PLAN_COMMIT = """chore({slug}): restore the approved plan

Why: tasks.md was edited inside the app after the owner approved it. The approved plan is put back (its run
log kept) so that what ships is governed by the approved contract; IT is told, because the orchestrator may
have executed the edited version.

Run: auto
"""


class FactoryError(Exception):
    pass


TEXT_SUFFIXES = {
    ".py", ".toml", ".md", ".yml", ".yaml", ".txt", ".cfg", ".ini", ".json", "",
    ".ts", ".tsx", ".js", ".jsx", ".html", ".css",
}  # fmt: skip


class Foreman:
    def __init__(
        self,
        cfg: Config,
        radar: Radar,
        store: Store,
        runner: ClaudeRunner | None = None,
        executor: Executor = shell_executor,
        engine: ClaudoEngine | None = None,
        host: GitHost | None = None,
        locker: Callable[[Path], tuple[int, str]] | None = None,
        identity: IdentityProvider | None = None,
        roles: RoleMap | None = None,
        sandbox: Sandbox | None = None,
        unsafe_host: bool = False,
    ) -> None:
        self.cfg = cfg
        self.radar = radar
        self.store = store
        self.runner = runner
        self.executor = executor
        self.today: Callable[[], date] = date.today  # injectable clock (tests)
        self.engine = engine  # Claudo: validates every plan with its own plan-lint when present
        self.host = host  # git host for `publish` / `sync` (None = delivery is off)
        # Who is acting: None = self-declared roles (`--as` / `--by` are trusted); else verified + roles.toml.
        self.identity, self.roles = identity, roles or RoleMap()
        self.locker = locker or lock_dependencies  # `uv lock` for a new app (injectable: tests stay offline)
        # Where agent-written code runs (P1-7): the sandbox, or the host on an explicit --unsafe-host. The CLI
        # always passes one of the two; a library caller passing neither runs on the host, unrecorded.
        self.sandbox, self.unsafe_host = sandbox, unsafe_host

    # ------------------------------------------------------------------ intake
    def intake(self, title: str, idea: str, maturity: str = "poc", requester: str = "business") -> WorkItem:
        if maturity not in MATURITIES:
            raise FactoryError(f"maturity must be one of {MATURITIES}")
        if not title.strip() or not idea.strip():
            raise FactoryError("an idea needs a title and a description")
        item = WorkItem(
            slug=self.store.new_slug(title),
            title=title.strip(),
            idea=idea.strip(),
            maturity=maturity,
            requester=requester,
        )
        item.log("intake", f"submitted by {requester}")
        self.store.save(item)
        self.store.write(item, "idea.md", idea_md(item))
        return item

    # ------------------------------------------------------------ GitHub issue intake (ROADMAP P2-5)
    def inbox(self) -> tuple[list[WorkItem], list[str]]:
        """Import the labelled issues of the intake repository as work items, then report every imported
        item's progress on its issue. Returns (new items, skipped issue lines). Idempotent: an issue is
        imported once."""
        host = self._host()
        if not self.cfg.intake_repo:
            raise FactoryError('GitHub intake is off: set [intake] repo = "owner/name" in factory.toml')
        known = {i.issue_url for i in self.store.all() if i.issue_url}
        new, skipped = [], []
        try:
            issues = host.list_issues(self.cfg.intake_repo, self.cfg.intake_label)
        except DeliveryError as e:
            raise FactoryError(str(e)) from e
        for issue in sorted(issues, key=lambda i: i["number"]):
            if issue["url"] in known:
                continue
            reason = self._issue_refusal(issue)
            if reason:
                skipped.append(f"#{issue['number']} {issue['title']!r}: {reason}")
                continue
            item = self.intake(
                issue["title"],
                issue["body"].strip() or issue["title"],
                self._issue_maturity(issue),
                requester=issue["author"] or "business",
            )
            item.issue_url = issue["url"]
            item.log("intake", f"from GitHub issue {issue['url']}")
            self.store.save(item)
            new.append(item)
        self.report_issues()
        return [self.store.load(i.slug) for i in new], skipped  # as reported (issue_reported updated)

    def _issue_refusal(self, issue: dict) -> str:
        if not issue["title"].strip():
            return "no title"
        if self.identity is not None and not self.roles.holds(issue["author"], "business"):
            return f"author {issue['author'] or '?'} does not hold the business role in roles.toml"
        return ""

    @staticmethod
    def _issue_maturity(issue: dict) -> str:
        for label in issue["labels"]:
            if label.lower().startswith("maturity:") and label.split(":", 1)[1].strip().lower() in MATURITIES:
                return label.split(":", 1)[1].strip().lower()
        m = re.search(r"(?im)^\s*maturity\s*:\s*(pov|poc|mvp|prod)\b", issue["body"])
        return m.group(1).lower() if m else "poc"

    def report_issues(self) -> int:
        """Comment on each imported item's issue when its stage or status changed since the last comment."""
        host = self._host()
        posted = 0
        for item in self.store.all():
            if not item.issue_url:
                continue
            state = f"{item.stage}/{item.status}"
            if state == item.issue_reported:
                continue
            try:
                host.comment_issue(item.issue_url, self._issue_comment(item))
            except DeliveryError as e:
                raise FactoryError(str(e)) from e
            item.issue_reported = state
            item.log("issue_comment", f"reported {state} on {item.issue_url}")
            self.store.save(item)
            posted += 1
        return posted

    def _issue_comment(self, item: WorkItem) -> str:
        if not item.issue_reported:
            head = (
                f"Received by the AI software factory as work item `{item.slug}` "
                f"(maturity `{item.maturity}`)."
            )
        else:
            head = f"Work item `{item.slug}` moved on."
        lines = [head, "", f"- Stage: **{describe_step(item.stage)}**", f"- Status: {item.status}"]
        if item.status == "waiting" and item.step.role:
            lines.append(f"- Waiting for: the **{item.step.role}** decision")
        if item.repo_url:
            lines.append(f"- Repository: {item.repo_url.removesuffix('.git')} (private)")
        if item.pr_url:
            lines.append(f"- Pull request: {item.pr_url}")
        if item.status == "blocked" and item.feedback:
            lines.append(f"- Blocked: {item.feedback.splitlines()[0][:200]}")
        lines += ["", "_Automatic update; the decisions stay with the people who hold each role._"]
        return "\n".join(lines)

    def _shipped_target(self, target: str) -> WorkItem:
        """The shipped app a change works on, or a FactoryError saying why it cannot."""
        try:
            app_item = self.store.load(target)
        except KeyError:
            raise FactoryError(f"no app '{target}' in this factory") from None
        if app_item.kind != "app" or app_item.status != "shipped":
            raise FactoryError(f"'{target}' is not a shipped app (status: {app_item.status}): ship it first")
        if not (self.cfg.apps_dir / target).is_dir():
            raise FactoryError(f"the folder of app '{target}' is missing: {self.cfg.apps_dir / target}")
        return app_item

    def intake_change(
        self, target: str, title: str, idea: str, kind: str = "feature", requester: str = "business"
    ) -> WorkItem:
        """A feature, bug fix or migration for an EXISTING shipped app, through the same governed pipeline."""
        if kind not in CHANGE_KINDS:
            raise FactoryError(f"a change is one of {CHANGE_KINDS}, not {kind!r}")
        if not title.strip() or not idea.strip():
            raise FactoryError("a change needs a title and a description")
        app_item = self._shipped_target(target)
        open_change = next((i for i in self.store.all() if i.change_open and i.target == target), None)
        if open_change is not None:  # one change at a time per app: they share one git branch space
            raise FactoryError(f"'{target}' already has an open change: {open_change.slug}: merge it first")
        item = WorkItem(
            slug=self.store.new_slug(title),
            title=title.strip(),
            idea=idea.strip(),
            maturity=app_item.maturity,
            requester=requester,
            kind=kind,
            target=target,
        )
        item.log("intake", f"{kind} of {target}, submitted by {requester}")
        self.store.save(item)
        self.store.write(item, "idea.md", idea_md(item))
        return item

    def open_migration(self, drift: Drift) -> WorkItem | None:
        """Open a migration for a drifted app. Idempotent: None if a change is already open for that app."""
        if any(i.change_open and i.target == drift.item.slug for i in self.store.all()):
            return None
        title, idea = migration_idea(drift, self.radar)
        item = self.intake_change(drift.item.slug, title, idea, "migration", requester="radar-drift")
        item.log("intake", "opened by `factory drift --open`")
        self.store.save(item)
        return item

    # ------------------------------------------------------------------ driving
    def run(self, item: WorkItem, max_steps: int = 20) -> WorkItem:
        """Run automatic stages until a checkpoint, a failure, or shipped."""
        if item.kind != "app":
            try:
                self._shipped_target(item.target)
            except FactoryError as e:  # the app it changes is gone or no longer shipped: nothing to build on
                item.status, item.feedback = "blocked", str(e)
                self.store.save(item)
                return item
        if item.status == "blocked":
            item.status = "active"
            item.log("retry", "resuming after block")
        builds_this_run = 0  # the automatic retry budget is per `run`: a human re-run grants a fresh one
        for _ in range(max_steps):
            step = item.step
            if step.kind == "terminal":
                item.status = "shipped"
                break
            if step.kind == "checkpoint":
                item.status = "waiting"
                item.log("waiting", f"{step.role}: {step.summary}")
                break
            if step.name == "build":
                refusal = self._containment(item)
                if refusal:  # not an attempt: nothing ran
                    item.status, item.feedback = "blocked", refusal
                    item.log("blocked", refusal[:600])
                    break
                builds_this_run += 1
                item.build_attempts += 1
            handler = getattr(self, f"_do_{step.name}")
            ok, detail = handler(item)
            item.log("done" if ok else "failed", detail[:600])
            if not ok:
                item.feedback = detail
                # Only an agent can act on the gate report; offline, a retry would fail identically.
                if (
                    step.name == "gate"
                    and self.runner is not None
                    and builds_this_run < self.cfg.max_build_attempts
                ):
                    item.log("retry", f"build attempt {builds_this_run + 1}/{self.cfg.max_build_attempts}")
                    item.stage, item.status = "build", "active"
                    continue
                item.status = "blocked"
                break
            self._advance(item)
        self.store.save(item)
        return item

    def _advance(self, item: WorkItem) -> None:
        idx = [s.name for s in STEPS].index(item.stage)
        nxt = STEPS[idx + 1]
        if nxt.name == "design_review" and item.skip_design_review:
            item.log("skipped", "design_review: stack fully adopted at this maturity")
            self._record_hash(item, "design.md")  # no human review: the compiled design is the approved one
            nxt = STEPS[idx + 2]
        item.stage = nxt.name
        item.status = "active"

    def _actor(self, role: str, by: str) -> tuple[str, str]:
        """(name, how it was verified) of whoever acts in `role`. Without an identity provider the name is
        self-declared (`--by`). With one, it is the verified account, which must hold `role` in roles.toml;
        `--by` may only repeat it."""
        if self.identity is None:
            return by or role, ""
        try:
            who = self.identity.current()
        except IdentityError as e:
            raise FactoryError(str(e)) from e
        if not self.roles.holds(who.login, role):
            held = ", ".join(self.roles.roles_of(who.login)) or "none"
            raise FactoryError(
                f"{who.login} ({who.source}) does not hold the role '{role}' in roles.toml (holds: {held})"
            )
        if by and by.lower() != who.login.lower():
            raise FactoryError(f"--by {by!r} is not the verified identity {who.login!r} ({who.source})")
        return who.login, who.source

    def _check_four_eyes(self, item: WorkItem, role: str, who: str) -> None:
        """Separation of duties: one person never decides checkpoints for two different roles of an item."""
        if not self.cfg.four_eyes or self.identity is None:
            return
        other = next(
            (a for a in item.approvals if a.get("by", "").lower() == who.lower() and a.get("role") != role),
            None,
        )
        if other:
            raise FactoryError(
                f"four-eyes: {who} already decided '{other['stage']}' as {other['role']}; another person "
                f"must decide as {role}"
            )

    def _require_checkpoint(self, item: WorkItem, role: str) -> None:
        if role not in ROLES:
            raise FactoryError(f"role must be one of {ROLES}")
        step = item.step
        if step.kind != "checkpoint":
            raise FactoryError(f"'{item.slug}' is at stage '{item.stage}', not at a checkpoint")
        if step.role != role:
            raise FactoryError(f"stage '{item.stage}' must be decided by '{step.role}', not '{role}'")

    def approve(self, item: WorkItem, role: str, by: str = "", note: str = "") -> WorkItem:
        self._require_checkpoint(item, role)
        by, verified = self._actor(role, by)
        self._check_four_eyes(item, role, by)
        if item.stage == "design_review":
            pending = self._design_report(item).needs_approval()
            granted = {v.key for v in pending} - item.active_exceptions(self.today())
            item.it_exceptions = sorted(set(item.it_exceptions) | granted)
            for key in granted:  # an exception is a debt: it lapses unless the policy says never
                self._set_terms(item, key, None, f"approved at design review: {note}".strip(": "), by or role)
        if item.stage == "spec_review":
            self._record_hash(item, "spec.md")  # the business contract, frozen at its approval
        if item.stage == "design_review":
            self._record_hash(item, "design.md")
        if item.stage == "plan_review":
            self._mark_plan_approved(item, by)
            self._record_hash(item, "tasks.md")  # the plan, frozen at its approval (run-log rows excluded)
        if item.stage == "ship_review" and not note.strip():
            # The human decides, but not blind: shipping over the reviewer's objection, or over something the
            # factory had to flag, must be justified, and the justification is recorded with the approval.
            reasons = []
            if item.claudo_review.get("verdict") not in (None, "PASS"):
                reasons.append(
                    f"Claudo's reviewer said {item.claudo_review['verdict']} (see {self.report_path(item)})"
                )
            reasons += [f"{a['kind']}: {a['detail']}" for a in item.ship_acks]
            if reasons:
                raise FactoryError(
                    "; ".join(reasons) + ': to ship anyway, approve with --note "why this is acceptable"; '
                    "or reject with --reason to have it reworked"
                )
        if item.stage == "ship_review":
            self._check_gated_head(item)
        if item.stage == "ship_review" and item.claudo_cp and not item.claudo_cp_consumed:
            outcome, detail = self._finalize_with_claudo(item, by or role)
            if outcome == "blocked":  # signing, the final run, or what it changed failed: nothing ships
                item.log("failed", detail[:600])
                item.feedback, item.status = detail, "blocked"
                self.store.save(item)
                return item
            if outcome == "redecide":  # the verdict IT approved is not the one that stands: ask again
                item.log("redecide", detail[:600])
                item.feedback, item.status = detail, "waiting"
                self.store.save(item)
                return item
        elif item.stage == "ship_review":
            self._seal_approved_head(item)
            item.claudo_cp, item.claudo_cp_consumed = "", False  # a re-decision approved: Claudo is done
        item.approvals.append(
            {"stage": item.stage, "role": role, "by": by or role, "note": note, "verified": verified}
        )
        item.log("approved", f"{role} {by}".strip() + (f": {note}" if note else ""))
        item.feedback = ""
        self._advance(item)
        return self.run(item)

    def _check_gated_head(self, item: WorkItem) -> None:
        """IT approves exactly what the gates judged: refuse if the app moved or got dirty since."""
        app = self.app_dir(item)
        if not app.is_dir():
            return
        if not item.gated_sha:
            raise FactoryError(
                "the gates never judged a commit of this app (it was gated as a loose folder, before the "
                "factory kept every app in git): reject it so it is rebuilt and gated again: "
                f'`factory reject {item.slug} --as it --reason "..."`'
            )
        if head_sha(app) != item.gated_sha:
            raise FactoryError(
                f"the app changed after the gates ran (gated {item.gated_sha[:8]}, now "
                f"{head_sha(app)[:8]}): reject it so it is rebuilt and gated again: "
                f'`factory reject {item.slug} --as it --reason "..."`'
            )
        dirty = porcelain(app)
        if dirty:
            raise FactoryError(
                f"uncommitted changes since the gates ran: {', '.join(dirty[:6])}; discard them, or reject "
                "the item so it is rebuilt and gated again: "
                f'`factory reject {item.slug} --as it --reason "..."`'
            )

    def _seal_approved_head(self, item: WorkItem) -> None:
        """Record the commit IT approves (the gated one: _check_gated_head ran just before)."""
        app = self.app_dir(item)
        if app.is_dir():
            item.approved_head = head_sha(app)

    def reject(self, item: WorkItem, role: str, reason: str, by: str = "") -> WorkItem:
        self._require_checkpoint(item, role)
        by, _ = self._actor(role, by)
        if not reason.strip():
            raise FactoryError("a rejection needs a reason (it is fed to the next attempt)")
        back = item.step.on_reject
        if item.stage == "ship_review" and item.claudo_cp:  # Claudo reopens the checkpoint's tasks
            item.claudo_rejection = {"cp": item.claudo_cp, "reason": reason, "by": by or role}
        item.log("rejected", f"{role} {by}".strip() + f": {reason}")
        item.feedback = reason
        item.stage = back
        item.status = "active"
        self.store.save(item)
        return item

    def _set_terms(self, item: WorkItem, key: str, expires: date | None, reason: str, by: str) -> None:
        """Record why, by whom and until when. `expires=None` means the policy default (exception_days)."""
        if expires is None and self.cfg.exception_days:
            expires = self.today() + timedelta(days=self.cfg.exception_days)
        item.exception_terms[key] = {
            "expires": expires.isoformat() if expires else "",
            "reason": reason,
            "by": by,
        }

    def allow(
        self,
        item: WorkItem,
        tech: str,
        role: str,
        by: str = "",
        expires: date | None = None,
        reason: str = "",
    ) -> WorkItem:
        """IT grants a radar exception for this item (e.g. a trial tech at MVP), with its terms."""
        if role != "it":
            raise FactoryError("only IT can grant a tech radar exception")
        by, _ = self._actor(role, by)
        if expires is not None and expires < self.today():
            raise FactoryError(f"an exception cannot expire in the past ({expires.isoformat()})")
        known = self.radar.get(tech) or self.radar.find(tech)
        key = known.id if known else tech
        if known and known.ring == "hold":
            raise FactoryError(
                f"{known.name} is on hold: change the radar (radar.toml) instead of an exception"
            )
        if key not in item.it_exceptions:
            item.it_exceptions.append(key)
        self._set_terms(item, key, expires, reason, by or role)  # renewing = allowing again with a new date
        until = item.expiry_of(key) or "no expiry"
        item.log("exception", f"IT {by} allowed {key} until {until}: {reason}".replace("  ", " "))
        self.store.save(item)
        return item

    def promote(self, item: WorkItem, to: str, role: str, by: str = "") -> WorkItem:
        """POV -> POC -> MVP -> prod: back through design with the stricter rules."""
        if role != "it":
            raise FactoryError("only IT can promote an app to a higher maturity")
        by, _ = self._actor(role, by)
        if item.kind != "app":
            raise FactoryError("only an app is promoted; a change inherits its app's maturity")
        if item.stage != "shipped":
            raise FactoryError("only a shipped item can be promoted")
        if MATURITIES.index(to) <= MATURITIES.index(item.maturity):
            raise FactoryError(f"cannot promote from {item.maturity} to {to}")
        item.log("promoted", f"{item.maturity} -> {to} by IT {by}".strip())
        item.maturity = to
        item.it_exceptions, item.exception_terms = [], {}  # granted for the previous rung
        item.stage = "design"
        item.status = "active"
        return self.run(item)

    # ------------------------------------------------------------------ stages
    def _ask_agent(self, item: WorkItem, prompt: str, role: str) -> tuple[bool, str]:
        assert self.runner is not None
        result = self.runner.run(
            prompt,
            cwd=self.store.dir(item.slug),
            model=self.cfg.models.get(role),
            tools=READ_ONLY_TOOLS,
            max_turns=8,
        )
        item.cost_usd += result.cost_usd
        if not result.ok or not result.text.strip():
            return False, f"{role} agent failed: {result.error or 'empty answer'}"
        return True, strip_fences(result.text)

    def _llm_capabilities(self, item: WorkItem, found: list[str]) -> list[str]:
        """Optional capabilities a model reads in the idea that the keywords missed (ROADMAP P3-9). Each
        must be justified by a verbatim quote of the idea; anything else is dropped. Keywords stay the floor;
        the technology choice stays deterministic (the radar compiles the design)."""
        offered = [c for c in OPTIONAL_CAPABILITIES if self.radar.by_category(c)]
        if self.runner is None or not offered or not self.cfg.capability_analyst:
            return []
        result = self.runner.run(
            capabilities_prompt(item.idea, offered),
            cwd=self.store.dir(item.slug),
            model=self.cfg.models.get("triage"),
            tools=[],
            max_turns=1,
        )
        item.cost_usd += result.cost_usd
        if not result.ok:
            item.log("capabilities", f"analyst failed, keywords only: {result.error[:200]}")
            return []
        accepted, problems = grounded_capabilities(result.text, item.idea, offered)
        added = [c for c in accepted if c not in found]
        detail = f"analyst added {', '.join(added)}" if added else "analyst agreed with the keywords"
        item.log("capabilities", detail + (f" ({'; '.join(problems)})" if problems else ""))
        return added

    def _do_triage(self, item: WorkItem) -> tuple[bool, str]:
        item.capabilities = detect_capabilities(item.idea)
        item.capabilities += self._llm_capabilities(item, item.capabilities)
        notes = []
        for tech in self.radar.scan_text(item.idea):
            v = verdict(tech, item.maturity)
            if v == BLOCK:
                alt = self.radar.get(tech.replaced_by) if tech.replaced_by else None
                notes.append(
                    f"business mentioned {tech.name} ({tech.ring}): not allowed"
                    + (f", will use {alt.name}" if alt else ", IT must propose an alternative")
                )
            else:
                notes.append(f"business mentioned {tech.name} ({tech.ring}): {v}")
        item.notes = notes
        return True, f"capabilities: {', '.join(item.capabilities)}" + (
            f"; {'; '.join(notes)}" if notes else ""
        )

    def _do_spec(self, item: WorkItem) -> tuple[bool, str]:
        """Write spec.md. It must pass the structural lint before a human sees it: an agent spec that fails is
        re-prompted with the errors (spec_lint_retries), then blocked."""
        feedback, tries = item.feedback, 0
        while True:
            if item.kind != "app":
                existing = self._read_app_file(item, f"work/{item.target}/spec.md")
                offline = offline_change_spec(item)
                prompt = change_spec_prompt(item, feedback, existing)
            else:
                offline, prompt = offline_spec(item), spec_prompt(item, feedback)
            if self.runner is None:
                text = offline
            else:
                ok, text = self._ask_agent(item, prompt, "spec")
                if not ok:
                    return False, text
            lint = lint_spec(text, kind=item.kind)
            if lint.ok:
                break
            if self.runner is None or tries >= self.cfg.spec_lint_retries:
                self.store.write(item, "spec.md", text)  # kept for the human to inspect
                where = "offline template" if self.runner is None else f"{tries + 1} attempt(s)"
                return False, f"spec failed the structural lint ({where}):\n{lint.feedback()}"
            tries += 1
            item.log("lint", f"spec lint: {len(lint.errors)} error(s), re-prompting ({tries})")
            feedback = (
                f"{item.feedback}\nYour previous spec.md FAILED the structural lint. Fix every error:\n"
                f"{lint.feedback()}\n\nYour previous spec.md was:\n{text}"
            )
        self.store.write(item, "spec.md", text)
        if lint.warnings:
            self.store.write(item, "spec-lint.md", f"# Spec lint (warnings)\n\n{lint.feedback()}\n")
        self.judge_artifact(item, "spec")
        mode = "offline template" if self.runner is None else f"agent, {tries} lint retries"
        return True, f"spec.md written ({mode})"

    def _design_report(self, item: WorkItem):
        d = self.store.dir(item.slug)
        return check_project(d, self.radar, item.maturity, docs=[d / "design.md"])

    def _read_app_file(self, item: WorkItem, rel: str) -> str:
        path = self.app_dir(item) / rel
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def _do_design_change(self, item: WorkItem) -> tuple[bool, str]:
        existing = existing_stack(self.app_dir(item), self.radar)
        text = render_change_design(
            slug=item.slug,
            title=item.title,
            kind=item.kind,
            target=item.target,
            maturity=item.maturity,
            radar=self.radar,
            existing=existing,
            gates=self.cfg.gates_for(item.maturity),
            spec_version="0.1.0",
        )
        self.store.write(item, "design.md", text)
        report = self._design_report(item)
        blocks = [v for v in report.violations if v.verdict == BLOCK]
        if blocks:  # cannot happen for a well-formed change design: blocked techs sit in the ignore block
            return False, "design violates the radar: " + "; ".join(v.describe() for v in blocks)
        item.skip_design_review = item.maturity in ("pov", "poc") and not report.needs_approval()
        migrating = [t.id for t in existing.techs if verdict(t, item.maturity) == BLOCK]
        note = f"; migrating away from: {', '.join(migrating)}" if migrating else ""
        return (
            True,
            f"existing stack of {item.target}: {', '.join(t.id for t in existing.techs) or '-'}{note}",
        )

    def _do_design(self, item: WorkItem) -> tuple[bool, str]:
        if item.kind != "app":
            return self._do_design_change(item)
        mentioned = self.radar.scan_text(item.idea)
        choice = choose_stack(
            self.radar, item.capabilities or detect_capabilities(item.idea), item.maturity, mentioned
        )
        text = render_design(
            slug=item.slug,
            title=item.title,
            maturity=item.maturity,
            radar=self.radar,
            choice=choice,
            gates=self.cfg.gates_for(item.maturity),
            spec_version="0.1.0",
            golden_deps=self._golden_deps(item),
        )
        self.store.write(item, "design.md", text)
        report = self._design_report(item)
        blocks = [v for v in report.violations if v.verdict == BLOCK]
        if blocks:
            return False, "design violates the radar: " + "; ".join(v.describe() for v in blocks)
        # POV/POC with a fully allowed stack: IT does not need to look at it.
        item.skip_design_review = (
            item.maturity in ("pov", "poc") and not report.needs_approval() and not choice.gaps
        )
        stack = ", ".join(f"{c}={t.id}" for c, t in choice.stack.items())
        extra = (
            f"; needs IT approval: {', '.join(t.id for t in choice.needs_approval)}"
            if choice.needs_approval
            else ""
        )
        gaps = f"; radar gaps: {', '.join(choice.gaps)}" if choice.gaps else ""
        return True, f"stack from radar: {stack}{extra}{gaps}"

    def _lint(self, item: WorkItem, tasks: str) -> LintResult | None:
        """Claudo's plan-lint (when available) plus the radar check on the plan itself. None = nothing
        to report because there is no engine and the plan names no forbidden technology."""
        result = LintResult()
        if self.engine is not None:
            s = self.store
            result = self.engine.lint_plan(
                item.slug, spec=s.read(item, "spec.md"), design=s.read(item, "design.md"), tasks=tasks
            )
        radar_errors = plan_radar_errors(tasks, self.radar, item.maturity)
        result.errors.extend(radar_errors)
        # The scope check is a heuristic: it feeds the re-prompt and warns the owner, it never blocks a plan.
        scope = plan_scope_errors(tasks)
        result.warnings.extend(scope)
        return None if self.engine is None and not radar_errors and not scope else result

    def _do_plan(self, item: WorkItem) -> tuple[bool, str]:
        """Write tasks.md. With Claudo available the plan must pass ITS plan-lint before a human sees
        it: an agent plan is re-prompted with the lint errors (plan_lint_retries), then blocked."""
        spec_ids = sorted(set(SPEC_ID.findall(self.store.read(item, "spec.md"))))
        feedback, lint, tries = item.feedback, None, 0
        while True:
            if self.runner is None:
                text = (
                    offline_change_tasks(item)
                    if item.kind != "app"
                    else offline_tasks(item, self._golden_path(item))
                )
            else:
                prompt = plan_prompt(
                    item,
                    feedback,
                    spec_ids,
                    self._scaffold_files(item),
                    change_of=item.target if item.kind != "app" else None,
                )
                ok, text = self._ask_agent(item, prompt, "plan")
                if not ok:
                    return False, text
            lint = self._lint(item, text)
            scope = [w for w in lint.warnings if "files_touched does not list it" in w] if lint else []
            if lint is None or (lint.ok and not scope):
                break
            if lint.ok and (self.runner is None or tries >= self.cfg.plan_lint_retries):
                break  # only scope warnings left: the owner sees them at plan review, nothing blocks
            if self.runner is None or tries >= self.cfg.plan_lint_retries:
                self.store.write(item, "tasks.md", text)  # keep it for the human to inspect
                where = "offline template" if self.runner is None else f"{tries + 1} attempt(s)"
                return False, f"plan rejected by Claudo plan-lint ({where}):\n{lint.feedback()}"
            tries += 1
            item.log(
                "lint",
                f"plan-lint: {len(lint.errors)} error(s), {len(scope)} scope warning(s), "
                f"re-prompting ({tries})",
            )
            feedback = (
                f"{item.feedback}\nYour previous tasks.md FAILED Claudo's plan-lint. Fix every error:\n"
                f"{lint.feedback()}\n\nYour previous tasks.md was:\n{text}"
            )
        self.store.write(item, "tasks.md", with_run_log(text))
        item.notes = [n for n in item.notes if not n.startswith("plan scope (heuristic):")]
        if lint is not None:
            self.store.write(item, "plan-lint.md", f"# Claudo plan-lint\n\n{lint.feedback() or 'clean'}\n")
            for warning in (w for w in lint.warnings if "files_touched does not list it" in w):
                item.notes.append(f"plan scope (heuristic): {warning}")
        self.judge_artifact(item, "plan")
        mode = "offline template" if self.runner is None else f"agent, {tries} lint retries"
        return True, f"tasks.md written ({mode})" + (", plan-lint ok" if lint is not None else "")

    def _mark_plan_approved(self, item: WorkItem, by: str) -> None:
        """Claudo refuses a plan whose frontmatter is not `status: approved`: the owner's approval at
        plan_review is exactly that statement, so write it into the artifact."""
        text = self.store.read(item, "tasks.md")
        if "status: proposed" not in text:
            return
        text = text.replace("status: proposed", f"status: approved\napproved_by: {by or 'owner'}", 1)
        self.store.write(item, "tasks.md", text)

    def _golden_path(self, item: WorkItem) -> str | None:
        """IT's template for this app: from the golden path manifests (golden.toml) when there are any, else
        the radar's `golden_path` of the chosen backend/frontend/language."""
        paths = load_golden_paths(self.cfg.golden_paths_dir)
        if paths:
            caps = item.capabilities or detect_capabilities(item.idea)
            chosen = pick_golden_path(paths, self.radar, caps, item.maturity)
            return chosen.name if chosen else None
        mentioned = self.radar.scan_text(item.idea)
        choice = choose_stack(
            self.radar, item.capabilities or detect_capabilities(item.idea), item.maturity, mentioned
        )
        for cap in ("backend", "frontend", "language"):
            tech = choice.stack.get(cap)
            if tech and tech.golden_path:
                return tech.golden_path
        return None

    # ------------------------------------------------------------------ judge (advisory)
    def _judge_inputs(self, item: WorkItem, kind: str) -> tuple[str, str]:
        """(artifact text, extra context) for a judge call; ('', '') when there is nothing to judge."""
        artifact, ctx = self._judge_material(item, kind)
        if item.kind != "app" and artifact:
            ctx = (
                f"\nCONTEXT: this {kind} is for a {item.kind} to the EXISTING app `{item.target}`. It "
                "describes only that delta: judge the delta against the idea. Rules that apply to NEW apps "
                "only (such as the mandated `GET /health` behaviour) do not apply to it.\n" + ctx
            )
        return artifact, ctx

    def _judge_material(self, item: WorkItem, kind: str) -> tuple[str, str]:
        if kind == "spec":
            return self.store.read(item, "spec.md"), ""
        if kind == "plan":
            ctx = f"\nSPEC the plan must implement:\n<spec>\n{self.store.read(item, 'spec.md')}\n</spec>\n"
            ctx += f"DESIGN (allowed stack):\n<design>\n{self.store.read(item, 'design.md')}\n</design>\n"
            return self.store.read(item, "tasks.md"), ctx
        app = self.app_dir(item)
        parts, size = [], 0
        for path in sorted([*app.glob("app/**/*.py"), *app.glob("tests/**/*.py")]):
            text = path.read_text(encoding="utf-8")
            parts.append(f"### {path.relative_to(app).as_posix()}\n{text}")
            size += len(text)
            if size > 40_000:
                break
        ctx = f"\nSPEC the code must satisfy:\n<spec>\n{self.store.read(item, 'spec.md')}\n</spec>\n"
        return "\n\n".join(parts), ctx

    def _judge_on(self, item: WorkItem) -> bool:
        """`judge = true` judges everything; `judge_from = "mvp"` judges items at that maturity and above."""
        if self.cfg.judge_enabled:
            return True
        floor = self.cfg.judge_from
        return bool(floor) and MATURITIES.index(item.maturity) >= MATURITIES.index(floor)

    def judge_artifact(self, item: WorkItem, kind: str, force: bool = False):
        """Run the LLM judge on an artifact. Never blocks: it is advice for the reviewer.

        Only with an agent runner and `[agent] judge = true` (billed), unless `force`
        (explicit `factory judge`)."""
        if self.runner is None or not (force or self._judge_on(item)):
            return None
        artifact, extra = self._judge_inputs(item, kind)
        if not artifact.strip():
            return None
        report = judge_panel(
            self.runner,
            kind,
            artifact,
            item.idea,
            votes=self.cfg.judge_votes,
            model=self.cfg.models.get("judge"),
            cwd=self.store.dir(item.slug),
            extra=extra,
            thinking_tokens=self.cfg.judge_thinking_tokens,
        )
        item.cost_usd += report.cost_usd
        item.judgements[kind] = {
            "verdict": report.verdict,
            "average": round(report.average, 2),
            "summary": report.summary,
            "votes": report.votes,
        }
        self.store.write(item, f"judge-{kind}.md", report.markdown())
        item.log("judged", report.short())
        return report

    def app_dir(self, item: WorkItem) -> Path:
        """Where the code lives: its own folder for a new app, the TARGET's folder for a change."""
        return self.cfg.apps_dir / (item.target if item.kind != "app" else item.slug)

    def _golden_deps(self, item: WorkItem) -> list[str]:
        """Runtime and dev dependencies the golden path ships: IT approved them by providing the template."""
        gp = self._golden_path(item)
        pyproject = self.cfg.golden_paths_dir / gp / "pyproject.toml" if gp else None
        if not pyproject or not pyproject.is_file():
            return []
        return list(dict.fromkeys(pyproject_dependencies(pyproject.read_text(encoding="utf-8"))))

    def _scaffold_files(self, item: WorkItem) -> list[str]:
        """What the app will already contain when the plan starts (the golden path's files, or for a change
        the files the existing app really has)."""
        if item.kind != "app":
            app = self.app_dir(item)
            skip = {".git", ".venv", "__pycache__", "node_modules", ".pytest_cache", ".ruff_cache", "work"}
            files = sorted(
                p.relative_to(app).as_posix()
                for p in app.rglob("*")
                if p.is_file() and not skip & set(p.relative_to(app).parts)
            )
            return files[:80]
        gp = self._golden_path(item)
        src = self.cfg.golden_paths_dir / gp if gp else None
        if not src or not src.is_dir():
            return []
        return sorted(p.relative_to(src).as_posix() for p in src.rglob("*") if p.is_file())

    def _uses_claudo(self, item: WorkItem) -> bool:
        """Claudo's per-task rigor costs several times the single-agent path (measured on a two-endpoint
        app: $3.29 for one task vs $0.71 for a whole app), so it engages from `build_from` maturity up,
        where an auditable task-by-task build earns its price."""
        return (
            self.engine is not None
            and self.runner is not None
            and MATURITIES.index(item.maturity) >= MATURITIES.index(self.cfg.claudo_build_from)
        )

    def _scaffold(self, item: WorkItem, app: Path) -> str:
        gp = self._golden_path(item)
        src = self.cfg.golden_paths_dir / gp if gp else None
        if not src or not src.is_dir():
            app.mkdir(parents=True, exist_ok=True)
            return "no golden path available: empty app folder"
        values = {"{{slug}}": item.slug, "{{title}}": item.title, "{{module}}": item.slug.replace("-", "_")}
        for path in sorted(src.rglob("*")):
            if path.is_dir():
                continue
            rel = path.relative_to(src)
            dest = app / rel
            if dest.exists():
                continue  # never clobber work from a previous attempt
            dest.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix in TEXT_SUFFIXES:
                text = path.read_text(encoding="utf-8")
                for k, v in values.items():
                    text = text.replace(k, v)
                dest.write_text(text, encoding="utf-8", newline="\n")
            else:
                shutil.copy2(path, dest)
        return f"scaffolded from golden_paths/{gp}"

    def _do_build_change(self, item: WorkItem) -> tuple[bool, str]:
        """Build a change ON A BRANCH of the existing app: the base stays untouched until IT merges."""
        app = self.app_dir(item)
        try:
            base, sha = begin_change(app, item.slug)
        except ProjectError as e:
            return False, str(e)
        if not item.base_branch:
            item.base_branch, item.base_sha = base, sha
        triplet = app / "work" / item.slug
        triplet.mkdir(parents=True, exist_ok=True)
        self._copy_triplet(item, triplet)
        commit_all(app, CHANGE_TRIPLET_COMMIT.replace("{slug}", item.slug))
        where = f"branch factory/{item.slug} from {item.base_branch}"
        if self.runner is None:
            return True, f"{where}; offline build (no agent: the app is unchanged)"
        if self._uses_claudo(item) and (not item.claudo_cp or item.claudo_rejection):
            return self._build_with_claudo(item, app, where)
        forbidden = [t.name for t in self.radar.techs if verdict(t, item.maturity) == BLOCK]
        result = self._builder(item).run(
            change_build_prompt(item, forbidden, item.feedback),
            cwd=app,
            model=self.cfg.models.get("build"),
            tools=BUILD_TOOLS,
            max_turns=self.cfg.max_turns_build,
            permission_mode="acceptEdits",
        )
        item.cost_usd += result.cost_usd
        if not result.ok:
            return False, f"{where}; build agent failed: {result.error}"
        self.store.write(item, "build-summary.md", result.text.strip() + "\n")
        message = CHANGE_BUILD_COMMIT.replace("{slug}", item.slug).replace("{kind}", item.kind)
        committed = commit_all(app, message)
        return True, f"{where}; built by agent (${result.cost_usd:.2f}), {len(committed)} file(s) changed"

    def merge(self, item: WorkItem, role: str, by: str = "") -> WorkItem:
        """IT merges an approved change into the app's base branch (fast-forward only).

        This is the human's explicit act: the factory itself never merges. A new app has nothing to merge."""
        if role != "it":
            raise FactoryError("only IT merges: the factory never merges on its own")
        by, _ = self._actor(role, by)
        if item.kind == "app":
            raise FactoryError("only a change to an existing app is merged; a new app is delivered as built")
        if item.merged:
            raise FactoryError(f"'{item.slug}' is already merged")
        if item.pr_url:
            raise FactoryError(
                f"'{item.slug}' has a pull request ({item.pr_url}): merge it there, then run `factory sync`"
            )
        if item.stage != "shipped":
            raise FactoryError(f"'{item.slug}' is not approved yet (it is at stage '{item.stage}')")
        try:
            published = self.store.load(item.target).repo
        except KeyError:
            published = ""
        if published:
            raise FactoryError(
                f"'{item.target}' is published at {published}: a local merge would diverge from it. "
                f"Publish this change as a pull request (`factory publish {item.slug} --as it`) instead"
            )
        try:
            tip = merge_fast_forward(self.app_dir(item), item.slug)
        except ProjectError as e:
            raise FactoryError(str(e)) from e
        item.merged = True
        self._move_target_head(item, tip)
        item.log("merged", f"IT {by}: {item.base_branch} is now at {tip[:8]}".replace("  ", " "))
        self.store.save(item)
        return item

    # ------------------------------------------------------------ delivery (private repos, pull requests)
    def _host(self) -> GitHost:
        if self.host is None:
            raise FactoryError('delivery is off: set [delivery] provider = "github" in factory.toml first')
        return self.host

    def publish(self, item: WorkItem, role: str, by: str = "", accept_unverified: bool = False) -> WorkItem:
        """IT publishes: a shipped NEW app becomes a private repository, an approved CHANGE a pull request.

        Explicit by design: nothing leaves the machine unless IT runs this. The factory opens the pull
        request; merging it is IT's act on the host."""
        if role != "it":
            raise FactoryError("only IT publishes: it is the step that leaves this machine")
        by, _ = self._actor(role, by)
        host = self._host()
        if item.stage != "shipped":
            raise FactoryError(f"'{item.slug}' is not approved yet (it is at stage '{item.stage}')")
        try:
            if item.kind == "app":
                self._publish_app(item, host, by, accept_unverified)
            else:
                self._publish_change(item, host, by, accept_unverified)
        except (DeliveryError, ProjectError) as e:
            raise FactoryError(str(e)) from e
        self.store.save(item)
        return item

    def _verify_publishable(
        self, item: WorkItem, app: Path, branch: str, rev_range: str, accept_unverified: bool
    ) -> None:
        """What leaves the machine is exactly the commit IT approved, and carries no secret in its history."""
        dirty = porcelain(app)
        if dirty:
            raise FactoryError(
                f"{app.name} has uncommitted changes never gated or approved: {', '.join(dirty[:6])}"
            )
        tip = rev_parse(app, branch)
        if not item.approved_head:
            if not accept_unverified:
                raise FactoryError(
                    f"'{item.slug}' was approved before approvals were tied to a commit, so {branch} "
                    f"({tip[:8]}) cannot be verified. Publish with --accept-unverified to take responsibility"
                )
            item.log(
                "unverified", f"IT accepted publishing {branch} at {tip[:8]} without a recorded approval"
            )
        elif tip != item.approved_head:
            raise FactoryError(
                f"{branch} is at {tip[:8]} but IT approved {item.approved_head[:8]}: the code moved "
                f"after the approval. See `git log {item.approved_head[:8]}..{branch}`; new work goes "
                f'through `factory change {item.target or item.slug} "title" --idea "..."` so it is '
                "gated and approved"
            )
        try:
            hits = secrets_in_history(app, rev_range)
        except ValueError as e:
            raise FactoryError(str(e)) from e
        if hits:
            raise FactoryError(
                "secrets found in the commits to publish:\n  "
                + "\n  ".join(hits[:10])
                + "\nremove them from "
                "the history before publishing: a later commit deleting them does not help"
            )

    def _publish_app(self, item: WorkItem, host: GitHost, by: str, accept_unverified: bool) -> None:
        app = self.app_dir(item)
        prepare_project(app)  # idempotent; an app gated before every app was kept in git has none yet
        branch = current_branch(app)
        if branch.startswith("factory/"):
            # A change is in flight: publish the app as it was BEFORE it (the change goes as a pull request).
            open_change = next((i for i in self.store.all() if i.change_open and i.target == item.slug), None)
            branch = open_change.base_branch if open_change and open_change.base_branch else "main"
        elif accept_unverified:
            commit_leftovers(app, item.slug)  # legacy item: IT takes responsibility for the tree as it is
        remote_branch = f"refs/remotes/origin/{branch}"
        rev_range = f"origin/{branch}..{branch}" if ref_exists(app, remote_branch) else branch
        self._verify_publishable(item, app, branch, rev_range, accept_unverified)
        if not item.repo_url:
            owner = self.cfg.delivery_owner or host.owner()
            name = f"{self.cfg.repo_prefix}{item.slug}"
            if host.repo_exists(owner, name):
                raise FactoryError(
                    f"{owner}/{name} already exists and was not created by this factory: refusing to push "
                    "into it. Choose another [delivery] repo_prefix, or push the app yourself"
                )
            description = f"{item.title} (built by the AI software factory; maturity {item.maturity})"
            item.repo_url = host.create_private_repo(owner, name, description)
            item.repo = f"{owner}/{name}"
            # Saved NOW: if the push below fails, the next publish must reuse this repository instead of
            # tripping over "already exists and was not created by this factory".
            item.log("repo_created", f"private repository {item.repo}")
            self.store.save(item)
        add_remote(app, item.repo_url)
        push_branch(app, branch, item.repo_url)
        item.log("published", f"IT {by}: private repository {item.repo}".replace("  ", " "))

    def _pr_body(self, item: WorkItem) -> str:
        spec = self.store.read(item, "spec.md")
        intent = item.idea
        if "## 1." in spec:  # the section's own heading line is dropped: the PR has its own "Intent" title
            section = spec.split("## 1.", 1)[1].split("## 2.", 1)[0]
            intent = section.partition("\n")[2].strip() or item.idea
        gates = [
            ln.removeprefix("## ")
            for ln in self.store.read(item, "gate-report.md").splitlines()
            if ln.startswith("## ")
        ]
        approvals = [
            f"- {a['stage']}: {a['role']} {a['by']}"
            + (f" (verified: {a['verified']})" if a.get("verified") else " (self-declared)")
            + (f" ({a['note']})" if a.get("note") else "")
            for a in item.approvals
        ]
        review = item.claudo_review
        lines = [
            f"**{item.kind.capitalize()}** to `{item.target}` (maturity `{item.maturity}`), "
            "built by the AI software factory.",
            "",
            "## Intent",
            intent[:1500],
            "",
            "## Gates",
            *([f"- {g}" for g in gates] or ["- (no gate report)"]),
        ]
        if item.build_where:
            where = (
                "sandbox (container, egress allowlist)"
                if item.build_where == "sandbox"
                else "host, unsandboxed"
            )
            lines += ["", f"Agent code ran in: **{where}**."]
        if review:
            lines += ["", f"## Claudo reviewer\n{review['cp']}: **{review['verdict']}** ({review['report']})"]
        if item.ship_acks:
            lines += ["", "## Needs IT attention", *[f"- {a['kind']}: {a['detail']}" for a in item.ship_acks]]
        if approvals:
            lines += ["", "## Approvals", *approvals]
        lines += [
            "",
            f"Cost: ${item.cost_usd:.2f}. Spec, design and plan: `work/{item.slug}/`.",
            "",
            "**The factory never merges.** Review and merge this pull request yourself, then `factory sync`.",
        ]
        return "\n".join(lines)

    def _publish_change(self, item: WorkItem, host: GitHost, by: str, accept_unverified: bool) -> None:
        if item.merged:
            raise FactoryError(f"'{item.slug}' is already merged")
        target = self._shipped_target(item.target)
        if not target.repo_url:
            raise FactoryError(f"publish the app first: `factory publish {item.target} --as it`")
        app = self.app_dir(item)
        head = f"factory/{item.slug}"
        base = item.base_branch or "main"
        rev_range = (
            f"origin/{head}..{head}" if ref_exists(app, f"refs/remotes/origin/{head}") else f"{base}..{head}"
        )
        self._verify_publishable(item, app, head, rev_range, accept_unverified)
        push_branch(app, head, target.repo_url)
        if item.pr_url:
            item.log("published", f"IT {by}: branch updated, pull request {item.pr_url}".replace("  ", " "))
            return
        owner, name = target.repo.split("/", 1)
        item.pr_url = host.open_pull_request(
            owner,
            name,
            f"factory/{item.slug}",
            item.base_branch or "main",
            f"[factory] {item.title}",
            self._pr_body(item),
        )
        item.pr_state = "OPEN"
        item.log("published", f"IT {by}: pull request {item.pr_url}".replace("  ", " "))

    def _move_target_head(self, change: WorkItem, tip: str) -> None:
        """A merged change is approved work: the app's base now stands at its approved head."""
        try:
            target = self.store.load(change.target)
        except KeyError:
            return
        target.approved_head = tip
        self.store.save(target)

    @staticmethod
    def _read_ci(item: WorkItem, host: GitHost) -> None:
        """The company CI on the pull request, read back (ROADMAP P2-4). Logged when it changes."""
        checks = host.pull_request_checks(item.pr_url)
        failed = [name for name, bucket in checks if bucket in ("fail", "cancel")]
        if not checks:
            state = "none"
        elif failed:
            state = "fail"
        elif any(bucket == "pending" for _, bucket in checks):
            state = "pending"
        else:
            state = "pass"
        if (state, failed) != (item.ci_state, item.ci_failed):
            item.log("ci", state + (f": {', '.join(failed)}" if failed else ""))
        item.ci_state, item.ci_failed = state, failed

    def sync(self, item: WorkItem) -> WorkItem:
        """Read the pull request's state; once IT merged it on the host, fast-forward the local app to it."""
        host = self._host()
        if item.kind == "app" or not item.pr_url:
            raise FactoryError(f"'{item.slug}' has no pull request: publish the change first")
        if item.status == "abandoned":
            raise FactoryError(f"'{item.slug}' was abandoned: there is nothing to sync")
        try:
            state = host.pull_request_state(item.pr_url)
            previous, item.pr_state = item.pr_state, state
            self._read_ci(item, host)
            if state == "MERGED" and not item.merged:
                tip = sync_merged_base(self.app_dir(item), item.slug, item.base_branch or "main")
                item.merged = True
                self._move_target_head(item, tip)
                item.log("merged", f"on the host; {item.base_branch} is now at {tip[:8]}")
                if item.ci_state == "fail":  # the factory never blocks a merge; it records this one
                    item.log("merged_over_red_ci", f"merged while CI failed: {', '.join(item.ci_failed)}")
            elif state == "CLOSED" and previous != "CLOSED":
                item.log("closed", "the pull request was closed without merging")
        except (DeliveryError, ProjectError) as e:
            raise FactoryError(str(e)) from e
        self.store.save(item)
        return item

    def _close_pull_request_for_abandon(self, item: WorkItem, reason: str, pr_closed: bool) -> None:
        """An abandoned change must not leave a pull request that could still be merged by mistake."""
        if pr_closed:  # IT closed it on the host by hand
            item.pr_state = "CLOSED"
            return
        if self.host is None:
            raise FactoryError(
                f"'{item.slug}' has a pull request ({item.pr_url}): enable delivery so the factory can close "
                "it, or close it on the host yourself and run abandon again with --pr-closed"
            )
        try:
            state = self.host.pull_request_state(item.pr_url)
            if state == "MERGED":
                item.pr_state = "MERGED"
                self.store.save(item)
                raise FactoryError(
                    f"'{item.slug}' was merged on the host: run `factory sync {item.slug}`, not abandon"
                )
            if state == "OPEN":
                self.host.close_pull_request(item.pr_url, f"Abandoned: {reason}")
        except DeliveryError as e:
            raise FactoryError(str(e)) from e
        item.pr_state = "CLOSED"

    def abandon(
        self, item: WorkItem, role: str, reason: str, by: str = "", pr_closed: bool = False
    ) -> WorkItem:
        """Drop a change nobody wants anymore, freeing the app for the next one (never a merged change)."""
        if role not in ("it", "owner"):
            raise FactoryError("only IT or the owner abandons a change")
        by, _ = self._actor(role, by)
        if item.kind == "app":
            raise FactoryError("only a change to an existing app can be abandoned")
        if item.merged:
            raise FactoryError(f"'{item.slug}' is already merged: revert it with a new change instead")
        if not reason.strip():
            raise FactoryError("abandoning needs a reason (it stays in the history)")
        app = self.app_dir(item)
        if (app / ".git").exists() and porcelain(app):  # check BEFORE touching the host (J-9)
            raise FactoryError(f"{app} has uncommitted changes: commit or discard them before abandoning")
        if item.pr_url and item.pr_state != "MERGED":
            self._close_pull_request_for_abandon(item, reason, pr_closed)
        try:
            abandon_change(self.app_dir(item), item.slug)
        except ProjectError as e:
            raise FactoryError(str(e)) from e
        item.status = "abandoned"
        item.log("abandoned", f"{role} {by}: {reason}".replace("  ", " "))
        self.store.save(item)
        return item

    def _copy_triplet(self, item: WorkItem, triplet: Path) -> None:
        """The approved artifacts travel with the code. tasks.md keeps the run log already in the app's copy:
        a retry re-copying the plan must not erase Claudo's bookkeeping."""
        for name in ("idea.md", "spec.md", "design.md", "tasks.md"):
            text = self.store.read(item, name)
            if not text:
                continue
            copy = triplet / name
            if name == "tasks.md" and copy.is_file():
                text = merge_plan_copy(text, copy.read_text(encoding="utf-8"))
            copy.write_text(text, encoding="utf-8", newline="\n")

    def _restore_edited_plan(self, item: WorkItem, app: Path) -> None:
        """An agent edited the approved plan inside the app: put the approved plan back before the gates
        run, deterministically (no agent run), keeping the run log, and flag it for IT: the orchestrator may
        have executed the edited version. A plan changed in the store is a human matter: the immutable gate
        says so."""
        approved = item.approved_hashes.get("tasks.md")
        copy = app / "work" / item.slug / "tasks.md"
        store_text = self.store.read(item, "tasks.md")
        if not approved or not copy.is_file() or self._contract_sha("tasks.md", store_text) != approved:
            return
        current = copy.read_text(encoding="utf-8")
        if self._contract_sha("tasks.md", current) == approved:
            return
        edited = sorted(set(plan_part(current).split("\n")) - set(plan_part(store_text).split("\n")))
        copy.write_text(merge_plan_copy(store_text, current), encoding="utf-8", newline="\n")
        rel = f"work/{item.slug}/tasks.md"
        if (app / ".git").exists() and rel in porcelain(app):
            commit_paths(app, [rel], RESTORE_PLAN_COMMIT.replace("{slug}", item.slug))
        sample = "; ".join(line.strip()[:80] for line in edited[:3] if line.strip()) or "lines removed"
        item.log("plan_restored", f"tasks.md edited inside the app, approved plan restored ({sample})")
        self._add_ack(
            item,
            "plan_edited",
            f"tasks.md was edited inside the app after its approval and has been restored ({sample}); the "
            "orchestrator may have run the edited plan: check the task commits",
        )

    def _do_build(self, item: WorkItem) -> tuple[bool, str]:
        if item.build_attempts <= 1:
            # A first build starts clean. Retries and reworks keep their acks: the commits that caused
            # them are still in the branch history, so IT must still acknowledge them.
            item.ship_acks = []
        if item.build_where == "host" and self.unsafe_host:
            self._add_ack(
                item, "unsafe_host", "built with --unsafe-host: agent code ran on the host, unsandboxed"
            )
            item.log("unsafe_host", "agent code runs on the host (--unsafe-host), outside the sandbox")
        if item.kind != "app":
            return self._do_build_change(item)
        app = self.app_dir(item)
        detail = self._scaffold(item, app)
        # The triplet travels with the app (Claudo layout: work/<feature>/).
        triplet = app / "work" / item.slug
        triplet.mkdir(parents=True, exist_ok=True)
        self._copy_triplet(item, triplet)
        if not (app / ".git").exists():
            # A new app is a git project BEFORE any agent touches it: its .git is the factory's (mounted
            # read-only in the sandbox), and every gate and approval judges a commit, never a loose tree.
            if self.runner is not None:  # the lockfile is part of the scaffold (no uv.lock scope drift later)
                rc, out = self.locker(app)
                item.log(
                    "lock",
                    "uv.lock created with the scaffold" if rc == 0 else f"uv lock failed ({rc}): {out}",
                )
            prepare_project(app)
        if self.runner is None:
            return True, f"{detail}; offline build (scaffold only, no agent)"
        # Claudo executes the approved plan task by task (DAG, per-task verify, evals, reviewer panel).
        # After a clean Claudo run, a failed FACTORY gate (radar, secrets...) is a targeted fix for one
        # agent below, not a replay of the whole plan; an IT rejection goes back through Claudo.
        if self._uses_claudo(item) and (not item.claudo_cp or item.claudo_rejection):
            return self._build_with_claudo(item, app, detail)
        forbidden = [t.name for t in self.radar.techs if verdict(t, item.maturity) == BLOCK]
        result = self._builder(item).run(
            build_prompt(item, forbidden, item.feedback),
            cwd=app,
            model=self.cfg.models.get("build"),
            tools=BUILD_TOOLS,
            max_turns=self.cfg.max_turns_build,
            permission_mode="acceptEdits",
        )
        item.cost_usd += result.cost_usd
        if not result.ok:
            return False, f"{detail}; build agent failed: {result.error}"
        self.store.write(item, "build-summary.md", result.text.strip() + "\n")
        if not item.claudo_cp:
            built = commit_all(app, APP_BUILD_COMMIT.replace("{slug}", item.slug))
            return True, f"{detail}; built by agent (${result.cost_usd:.2f}), {len(built)} file(s) committed"
        # A fix after Claudo's run: committed apart and shown to IT, because Claudo's reviewer never saw it.
        fixed = commit_all(app, FIX_AFTER_REVIEW_COMMIT.replace("{slug}", item.slug))
        if not fixed:
            return True, f"{detail}; the fix agent changed nothing (${result.cost_usd:.2f})"
        listed = ", ".join(fixed[:8]) + (f" (+{len(fixed) - 8} more)" if len(fixed) > 8 else "")
        self._add_ack(item, "fixed_after_review", f"{listed}: changed after Claudo's review")
        return True, f"{detail}; fixed by agent after Claudo's review (${result.cost_usd:.2f}): {listed}"

    def _containment(self, item: WorkItem) -> str:
        """Decide where this build's agent code runs (item.build_where); a refusal when it may not start.

        The sandbox whenever it is ready. Not ready: an MVP or prod build waits (or the human passes
        --unsafe-host); a POV/POC on a dev machine falls back to the host with a note (ROADMAP D5)."""
        if self.runner is None:
            item.build_where = ""
            return ""
        if self.sandbox is None:
            item.build_where = "host"
            return ""
        problems = self.sandbox.problems()
        if not problems:
            item.build_where = "sandbox"
            return ""
        if item.maturity in ("pov", "poc"):
            item.build_where = "host"
            note = f"built on the host: the sandbox is not ready ({problems[0]}); required from MVP up"
            if note not in item.notes:
                item.notes.append(note)
            item.log("unsandboxed", note)
            return ""
        return (
            f"the build sandbox is not ready (required for {item.maturity} builds):\n- "
            + "\n- ".join(problems)
            + "\nOr rerun with --unsafe-host to build on the host (recorded for IT to acknowledge)."
        )

    def _builder(self, item: WorkItem) -> ClaudeRunner:
        """The agent that writes code: the sandboxed one when this build is contained."""
        assert self.runner is not None
        if item.build_where == "sandbox" and self.sandbox is not None:
            return SandboxedRunner(self.sandbox)
        return self.runner

    def _gate_executor(self, item: WorkItem) -> Executor:
        """Gate commands run the agent's code: in the sandbox when the agent built it there."""
        if item.build_where == "sandbox" and self.sandbox is not None:
            return self.sandbox.executor
        return self.executor

    def _signing_secret(self) -> str:
        """Kept in the per-user state directory, not in the factory tree the build agents work next to."""
        migrate_legacy_secret(self.cfg.root)
        return load_or_create_secret(secret_path(self.cfg.root))

    def _claudo_env(self, item: WorkItem) -> dict[str, str]:
        env = {
            "LAB_APPROVAL_SECRET": self._signing_secret(),
            "LAB_APPROVAL_NONCE": item.approval_nonce,
            "LAB_REQUIRE_NONCE": "1",  # an unbound token is refused: replay protection is not optional
        }
        if self.cfg.claudo_budget_usd:
            env["LAB_BUDGET_USD"] = str(self.cfg.claudo_budget_usd)
        if item.build_where == "sandbox" and self.sandbox is not None:  # agents AND their verify/evals
            env.update(self.sandbox.claudo_env())
        return env

    def _commit_leftovers(self, item: WorkItem, app: Path) -> None:
        bookkeeping, drift = commit_leftovers_split(app, item.slug)
        if bookkeeping:
            item.log(
                "leftovers", f"committed {len(bookkeeping)} bookkeeping file(s): {', '.join(bookkeeping[:8])}"
            )
        if drift:
            listed = ", ".join(drift[:8]) + (f" (+{len(drift) - 8} more)" if len(drift) > 8 else "")
            item.log("scope_drift", f"{len(drift)} file(s) edited outside every task scope: {listed}")
            self._add_ack(item, "scope_drift", f"{listed}: edited outside every task's files_touched")

    @staticmethod
    def _add_ack(item: WorkItem, kind: str, detail: str) -> None:
        ack = {"kind": kind, "detail": detail}
        if ack not in item.ship_acks:
            item.ship_acks.append(ack)

    def _clean_tree_gate(self, item: WorkItem) -> GateResult:
        """What gets delivered is the git HEAD: every gate must have judged exactly that."""
        app = self.app_dir(item)
        if not (app / ".git").exists():
            return GateResult(
                "clean_tree", False, "the app is not a git project: nothing to deliver as a commit"
            )
        dirty = porcelain(app)
        detail = "uncommitted changes: " + ", ".join(dirty[:10]) if dirty else "HEAD is the delivered state"
        return GateResult("clean_tree", not dirty, detail)

    def _add_claudo_cost(self, item: WorkItem, app: Path) -> None:
        """Claudo's agents are billed outside the factory's own runner: read the spend from its journal."""
        assert self.engine is not None
        total = self.engine.journal_cost(app, item.slug)
        item.cost_usd += max(0.0, total - item.claudo_cost_seen)
        item.claudo_cost_seen = max(item.claudo_cost_seen, total)

    def _build_with_claudo(self, item: WorkItem, app: Path, scaffold: str) -> tuple[bool, str]:
        assert self.engine is not None
        prepare_project(app)  # git repo + local identity + `evals` recipe: what the orchestrator needs
        if item.claudo_rejection:  # IT said no at the ship review: Claudo reopens the tasks with the reason
            rej = item.claudo_rejection
            if item.claudo_cp_consumed:  # rejected after a re-decision: the checkpoint must be pending again
                self.engine.reopen_checkpoint(app, item.slug, rej["cp"])
                item.claudo_cp_consumed = False
            self.engine.reject_checkpoint(app, item.slug, rej["cp"], rej["reason"], rej["by"])
            item.claudo_rejection = {}
        # A new round of the checkpoint: a fresh nonce, so no token from an earlier round verifies again.
        item.approval_nonce = secrets.token_hex(16)
        if not self.engine.supports_nonce():
            warning = (
                "replay protection unavailable: this Claudo predates nonce-bound approval tokens "
                "(update it to b9b96c7 or later)"
            )
            if warning not in item.notes:
                item.notes.append(warning)
                item.log("warning", warning)
        self.store.save(item)
        res = self.engine.run_build(
            item.slug, app, env=self._claudo_env(item), timeout=self.cfg.claudo_timeout
        )
        self.store.write(item, "claudo-build.log", res.log[-30000:])
        self._add_claudo_cost(item, app)
        if res.ok:
            self._commit_leftovers(item, app)
        if res.outcome == "checkpoint":
            item.claudo_cp = res.checkpoint
            review = self.engine.review_verdict(app, item.slug, res.checkpoint)
            item.claudo_review = (
                {"cp": res.checkpoint, "verdict": review[0], "report": review[1]} if review else {}
            )
            return True, f"{scaffold}; Claudo built the plan, paused at {res.checkpoint}"
        if res.outcome == "done":
            item.claudo_cp = ""
            return True, f"{scaffold}; Claudo run complete"
        return False, f"{scaffold}; Claudo build {res.outcome}:\n{res.log[-2500:]}"

    def report_path(self, item: WorkItem) -> str:
        """Where to read Claudo's review report: in the app the item lives in (a change's is its target's)."""
        report = item.claudo_review.get("report", "")
        where = self.app_dir(item) / report
        try:
            return where.relative_to(self.cfg.root).as_posix()
        except ValueError:
            return str(where)

    def _review_report(self, item: WorkItem, app: Path) -> Path | None:
        report = item.claudo_review.get("report") if item.claudo_review else ""
        path = app / report if report else None
        return path if path is not None and path.is_file() else None

    def _finalize_with_claudo(self, item: WorkItem, by: str) -> tuple[str, str]:
        """IT approved the ship review: write the SIGNED approval Claudo is waiting for, let it finish.

        Returns ("ok" | "blocked" | "redecide", detail). The token is signed with a secret only the factory
        holds, bound to this round's nonce, and records WHO approved. What IT approved is remembered (HEAD
        and the review report it read): after Claudo's final run only its own bookkeeping may have changed,
        and a verdict that moved to something IT did not approve sends the decision back to IT."""
        assert self.engine is not None
        app = self.app_dir(item)
        item.approved_head = head_sha(app)
        report = self._review_report(item, app)
        item.approved_review_sha256 = self._sha(report.read_text(encoding="utf-8")) if report else ""
        approved_verdict = item.claudo_review.get("verdict")
        if not item.approval_nonce:  # paused before nonces existed: only signer and verifying run must agree
            item.approval_nonce = secrets.token_hex(16)
            self.store.save(item)
        secret = self._signing_secret()
        try:
            self.engine.sign_approval(app, item.slug, item.claudo_cp, by, secret, nonce=item.approval_nonce)
        except EngineError as e:
            return "blocked", str(e)
        res = self.engine.run_build(
            item.slug,
            app,
            stop_at_checkpoint=False,
            env=self._claudo_env(item),
            timeout=self.cfg.claudo_timeout,
        )
        self.store.write(item, "claudo-final.log", res.log[-30000:])
        self._add_claudo_cost(item, app)
        if res.ok:
            self._commit_leftovers(item, app)
        if res.outcome != "done":
            return "blocked", f"Claudo did not complete after approval ({res.outcome}):\n{res.log[-2500:]}"
        # Claudo has passed its checkpoint. If the ship is blocked or IT must decide again, a rejection has to
        # reopen that checkpoint first (see _build_with_claudo).
        item.claudo_cp_consumed = True
        if item.approved_head:
            changed = post_approval_changes(app, item.slug, item.approved_head)
            if changed:
                return "blocked", (
                    f"Claudo changed {', '.join(changed[:8])} after the approval: not shipped. "
                    "That content was never gated or seen by IT."
                )
        if (
            item.gated_sha
        ):  # only Claudo's bookkeeping moved HEAD (proven above): the gated content is unchanged
            item.gated_sha = head_sha(app)
        report = self._review_report(item, app)
        if report and self._sha(report.read_text(encoding="utf-8")) != item.approved_review_sha256:
            review = self.engine.review_verdict(app, item.slug, item.claudo_cp)
            if review:
                item.claudo_review = {"cp": item.claudo_cp, "verdict": review[0], "report": review[1]}
                if review[0] != "PASS" and review[0] != approved_verdict:
                    # IT decides again. claudo_cp stays (consumed), so that a rejection can still be handed to
                    # Claudo: its checkpoint is reopened first.
                    return "redecide", (
                        f"the reviewer re-ran after your approval and now says {review[0]} "
                        f"(you approved {approved_verdict}): read {self.report_path(item)} "
                        "and decide again"
                    )
        item.claudo_cp, item.claudo_cp_consumed = "", False
        item.approved_head = head_sha(app)  # moved only by Claudo's bookkeeping, proven just above
        return "ok", "approved"

    @staticmethod
    def _sha(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _contract_sha(self, name: str, text: str) -> str:
        """Hash of an approved artifact. Claudo appends run-log rows to tasks.md as it works; those rows are
        bookkeeping, not part of the approved plan, so they are left out (and CRLF is normalised)."""
        if name == "tasks.md":
            text = plan_part(text)  # the `## Run log` section (and Claudo's rows) is bookkeeping
        return self._sha(text)

    def _record_hash(self, item: WorkItem, name: str) -> None:
        item.approved_hashes[name] = self._contract_sha(name, self.store.read(item, name))

    def _immutability_gate(self, item: WorkItem) -> GateResult:
        """spec.md, design.md and tasks.md must be what a human approved. "Never edit the spec during a
        build" was only a prompt rule: an agent that weakens an eval in its own copy would pass every other
        gate. This makes the rule mechanical, for the store's copy and for the copy inside the app."""
        copies = self.app_dir(item) / "work" / item.slug
        problems = []
        for name in ("spec.md", "design.md", "tasks.md"):
            approved = item.approved_hashes.get(name)
            if not approved:
                continue  # item approved before hashes were recorded: nothing to compare against
            if self._contract_sha(name, self.store.read(item, name)) != approved:
                problems.append(
                    f"{name}: changed in work/{item.slug}/ after its approval; amend it through the review "
                    "checkpoint (reject, regenerate, approve again)"
                )
            copy = copies / name
            if not copy.is_file():
                problems.append(f"{name}: missing from the app (work/{item.slug}/{name})")
            elif self._contract_sha(name, copy.read_text(encoding="utf-8")) != approved:
                problems.append(
                    f"{name}: modified inside the app during the build; agents never edit the contract"
                )
        detail = "\n".join(problems) or "spec.md, design.md and tasks.md are as approved"
        return GateResult("immutable", not problems, detail)

    def _trajectory_gate(self, item: WorkItem) -> GateResult:
        if self.engine is None or not item.claudo_cp:
            return GateResult("trajectory", True, "not applicable: this item was not built through Claudo")
        ok, out = self.engine.trajectory(self.app_dir(item), item.slug)
        return GateResult("trajectory", ok, out or "ok")

    def _do_gate(self, item: WorkItem) -> tuple[bool, str]:
        app = self.app_dir(item)
        self._restore_edited_plan(item, app)
        results = run_gates(
            self.cfg.gates_for(item.maturity),
            app,
            radar=self.radar,
            maturity=item.maturity,
            exceptions=item.active_exceptions(self.today()),
            docs=[app / "work" / item.slug / "design.md"],
            commands={**self.cfg.gate_commands, **golden_gate_commands(app)},
            executor=self._gate_executor(item),
            extra={
                "immutable": lambda: self._immutability_gate(item),
                "trajectory": lambda: self._trajectory_gate(item),
                "clean_tree": lambda: self._clean_tree_gate(item),
            },
        )
        self.store.write(item, "gate-report.md", format_report(results, item.maturity))
        item.gated_sha = ""
        if item.kind != "app" and item.base_sha:
            item.ship_acks = [a for a in item.ship_acks if a["kind"] != "tests_modified"]
            rewritten = modified_tests(app, item.base_sha)
            if rewritten:
                listed = ", ".join(rewritten[:8]) + (
                    f" (+{len(rewritten) - 8} more)" if len(rewritten) > 8 else ""
                )
                self._add_ack(
                    item, "tests_modified", f"{listed}: existing tests were rewritten, not just added to"
                )
        failed = [r for r in results if not r.ok]
        if failed:
            item.stage = "build"  # the next build gets this detail as feedback
            detail = "\n\n".join(f"[{r.name}] {r.detail}" for r in failed)
            return False, "gates failed:\n" + detail[-3000:]
        item.gated_sha = head_sha(app)
        self.judge_artifact(item, "build")
        return True, "gates passed: " + ", ".join(r.name for r in results)


def golden_gate_commands(app: Path) -> dict[str, str]:
    """Gate command overrides the app's golden path carries in its own golden.toml (e.g. frontend tests)."""
    import tomllib

    manifest = app / "golden.toml"
    if not manifest.is_file():
        return {}
    data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    return {k: str(v) for k, v in data.get("gates", {}).get("commands", {}).items()}


def describe_step(stage: str) -> str:
    step = STEP_BY_NAME[stage]
    who = f" [{step.role}]" if step.role else ""
    return f"{stage}{who}: {step.summary}"
