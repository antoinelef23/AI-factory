"""The foreman: moves work items through the stages, stops at human checkpoints.

Automatic stages run back to back; a checkpoint parks the item (status `waiting`) until
the right role approves or rejects it. A failed stage parks it as `blocked` with the
reason in `feedback`, which the next attempt receives.
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

from factory.agents import BUILD_TOOLS, READ_ONLY_TOOLS, ClaudeRunner, strip_fences
from factory.claudo import ClaudoEngine, EngineError, LintResult, load_or_create_secret
from factory.config import Config
from factory.design import (
    CHANGE_KINDS,
    choose_stack,
    detect_capabilities,
    existing_stack,
    render_change_design,
    render_design,
)
from factory.detect import pyproject_dependencies
from factory.drift import Drift, migration_idea
from factory.gates import Executor, GateResult, format_report, run_gates, shell_executor
from factory.guard import check_project, plan_radar_errors
from factory.judge import judge
from factory.project import (
    CHANGE_BUILD_COMMIT,
    CHANGE_TRIPLET_COMMIT,
    ProjectError,
    abandon_change,
    begin_change,
    commit_all,
    commit_leftovers,
    merge_fast_forward,
    porcelain,
    prepare_project,
)
from factory.radar import BLOCK, MATURITIES, Radar, verdict
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
)
from factory.workitem import ROLES, STEP_BY_NAME, STEPS, Store, WorkItem


class FactoryError(Exception):
    pass


TEXT_SUFFIXES = {".py", ".toml", ".md", ".yml", ".yaml", ".txt", ".cfg", ".ini", ".json", ""}


class Foreman:
    def __init__(
        self,
        cfg: Config,
        radar: Radar,
        store: Store,
        runner: ClaudeRunner | None = None,
        executor: Executor = shell_executor,
        engine: ClaudoEngine | None = None,
    ) -> None:
        self.cfg = cfg
        self.radar = radar
        self.store = store
        self.runner = runner
        self.executor = executor
        self.today: Callable[[], date] = date.today  # injectable clock (tests)
        self.engine = engine  # Claudo: validates every plan with its own plan-lint when present

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
        if (
            item.stage == "ship_review"
            and item.claudo_review.get("verdict") not in (None, "PASS")
            and not note.strip()
        ):
            # The human decides, but not blind: shipping over the reviewer's objection must be justified, and
            # the justification is recorded with the approval.
            raise FactoryError(
                f"Claudo's reviewer said {item.claudo_review['verdict']} "
                f"(see apps/{item.slug}/{item.claudo_review['report']}): to ship anyway, approve with "
                '--note "why this is acceptable"; or reject with --reason to have it reworked'
            )
        if item.stage == "ship_review" and item.claudo_cp:
            ok, detail = self._finalize_with_claudo(item, by or role)
            if not ok:  # signing or the final orchestrator run failed: nothing ships
                item.log("failed", detail[:600])
                item.feedback, item.status = detail, "blocked"
                self.store.save(item)
                return item
        item.approvals.append({"stage": item.stage, "role": role, "by": by or role, "note": note})
        item.log("approved", f"{role} {by}".strip() + (f": {note}" if note else ""))
        item.feedback = ""
        self._advance(item)
        return self.run(item)

    def reject(self, item: WorkItem, role: str, reason: str, by: str = "") -> WorkItem:
        self._require_checkpoint(item, role)
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

    def _do_triage(self, item: WorkItem) -> tuple[bool, str]:
        item.capabilities = detect_capabilities(item.idea)
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
        if item.kind != "app":
            existing = self._read_app_file(item, f"work/{item.target}/spec.md")
            offline, prompt = offline_change_spec(item), change_spec_prompt(item, item.feedback, existing)
        else:
            offline, prompt = offline_spec(item), None
        if self.runner is None:
            text = offline
        else:
            ok, text = self._ask_agent(item, prompt or spec_prompt(item, item.feedback), "spec")
            if not ok:
                return False, text
        self.store.write(item, "spec.md", text)
        self.judge_artifact(item, "spec")
        return True, "spec.md written" + (" (offline template)" if self.runner is None else "")

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
        return None if self.engine is None and not radar_errors else result

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
            if lint is None or lint.ok:
                break
            if self.runner is None or tries >= self.cfg.plan_lint_retries:
                self.store.write(item, "tasks.md", text)  # keep it for the human to inspect
                where = "offline template" if self.runner is None else f"{tries + 1} attempt(s)"
                return False, f"plan rejected by Claudo plan-lint ({where}):\n{lint.feedback()}"
            tries += 1
            item.log("lint", f"plan-lint: {len(lint.errors)} error(s), re-prompting ({tries})")
            feedback = (
                f"{item.feedback}\nYour previous tasks.md FAILED Claudo's plan-lint. Fix every error:\n"
                f"{lint.feedback()}\n\nYour previous tasks.md was:\n{text}"
            )
        self.store.write(item, "tasks.md", text)
        if lint is not None:
            self.store.write(item, "plan-lint.md", f"# Claudo plan-lint\n\n{lint.feedback() or 'clean'}\n")
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

    def judge_artifact(self, item: WorkItem, kind: str, force: bool = False):
        """Run the LLM judge on an artifact. Never blocks: it is advice for the reviewer.

        Only with an agent runner and `[agent] judge = true` (billed), unless `force`
        (explicit `factory judge`)."""
        if self.runner is None or not (force or self.cfg.judge_enabled):
            return None
        artifact, extra = self._judge_inputs(item, kind)
        if not artifact.strip():
            return None
        report = judge(
            self.runner,
            kind,
            artifact,
            item.idea,
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
        for name in ("idea.md", "spec.md", "design.md", "tasks.md"):
            text = self.store.read(item, name)
            if text:
                (triplet / name).write_text(text, encoding="utf-8", newline="\n")
        commit_all(app, CHANGE_TRIPLET_COMMIT.replace("{slug}", item.slug))
        where = f"branch factory/{item.slug} from {item.base_branch}"
        if self.runner is None:
            return True, f"{where}; offline build (no agent: the app is unchanged)"
        if self._uses_claudo(item) and (not item.claudo_cp or item.claudo_rejection):
            return self._build_with_claudo(item, app, where)
        forbidden = [t.name for t in self.radar.techs if verdict(t, item.maturity) == BLOCK]
        result = self.runner.run(
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
        if item.kind == "app":
            raise FactoryError("only a change to an existing app is merged; a new app is delivered as built")
        if item.merged:
            raise FactoryError(f"'{item.slug}' is already merged")
        if item.stage != "shipped":
            raise FactoryError(f"'{item.slug}' is not approved yet (it is at stage '{item.stage}')")
        try:
            tip = merge_fast_forward(self.app_dir(item), item.slug)
        except ProjectError as e:
            raise FactoryError(str(e)) from e
        item.merged = True
        item.log("merged", f"IT {by}: {item.base_branch} is now at {tip[:8]}".replace("  ", " "))
        self.store.save(item)
        return item

    def abandon(self, item: WorkItem, role: str, reason: str, by: str = "") -> WorkItem:
        """Drop a change nobody wants anymore, freeing the app for the next one (never a merged change)."""
        if role not in ("it", "owner"):
            raise FactoryError("only IT or the owner abandons a change")
        if item.kind == "app":
            raise FactoryError("only a change to an existing app can be abandoned")
        if item.merged:
            raise FactoryError(f"'{item.slug}' is already merged: revert it with a new change instead")
        if not reason.strip():
            raise FactoryError("abandoning needs a reason (it stays in the history)")
        try:
            abandon_change(self.app_dir(item), item.slug)
        except ProjectError as e:
            raise FactoryError(str(e)) from e
        item.status = "abandoned"
        item.log("abandoned", f"{role} {by}: {reason}".replace("  ", " "))
        self.store.save(item)
        return item

    def _do_build(self, item: WorkItem) -> tuple[bool, str]:
        if item.kind != "app":
            return self._do_build_change(item)
        app = self.app_dir(item)
        detail = self._scaffold(item, app)
        # The triplet travels with the app (Claudo layout: work/<feature>/).
        triplet = app / "work" / item.slug
        triplet.mkdir(parents=True, exist_ok=True)
        for name in ("idea.md", "spec.md", "design.md", "tasks.md"):
            text = self.store.read(item, name)
            if text:
                (triplet / name).write_text(text, encoding="utf-8", newline="\n")
        if self.runner is None:
            return True, f"{detail}; offline build (scaffold only, no agent)"
        # Claudo executes the approved plan task by task (DAG, per-task verify, evals, reviewer panel).
        # After a clean Claudo run, a failed FACTORY gate (radar, secrets...) is a targeted fix for one
        # agent below, not a replay of the whole plan; an IT rejection goes back through Claudo.
        if self._uses_claudo(item) and (not item.claudo_cp or item.claudo_rejection):
            return self._build_with_claudo(item, app, detail)
        forbidden = [t.name for t in self.radar.techs if verdict(t, item.maturity) == BLOCK]
        result = self.runner.run(
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
        return True, f"{detail}; built by agent (${result.cost_usd:.2f})"

    def _signing_secret(self) -> str:
        """Kept outside every app (and git-ignored), so agents neither inherit nor find it."""
        return load_or_create_secret(self.cfg.root / ".factory" / "approval-secret")

    def _claudo_env(self) -> dict[str, str]:
        env = {"LAB_APPROVAL_SECRET": self._signing_secret()}
        if self.cfg.claudo_budget_usd:
            env["LAB_BUDGET_USD"] = str(self.cfg.claudo_budget_usd)
        return env

    def _commit_leftovers(self, item: WorkItem, app: Path) -> None:
        files = commit_leftovers(app, item.slug)
        if files:
            item.log(
                "leftovers",
                f"committed {len(files)} file(s) left outside the task scopes: {', '.join(files[:8])}",
            )

    def _clean_tree_gate(self, item: WorkItem) -> GateResult:
        """What gets delivered is the git HEAD: every gate must have judged exactly that."""
        app = self.app_dir(item)
        if not (app / ".git").exists():
            return GateResult("clean_tree", True, "not applicable: the app is not a git project")
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
            self.engine.reject_checkpoint(app, item.slug, rej["cp"], rej["reason"], rej["by"])
            item.claudo_rejection = {}
        res = self.engine.run_build(item.slug, app, env=self._claudo_env(), timeout=self.cfg.claudo_timeout)
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

    def _finalize_with_claudo(self, item: WorkItem, by: str) -> tuple[bool, str]:
        """IT approved the ship review: write the SIGNED approval Claudo is waiting for, let it finish.

        The token is signed with a secret only the factory holds, and records WHO approved: an agent
        cannot self-approve (Claudo discards an unsigned or forged token)."""
        assert self.engine is not None
        app = self.app_dir(item)
        secret = self._signing_secret()
        try:
            self.engine.sign_approval(app, item.slug, item.claudo_cp, by, secret)
        except EngineError as e:
            return False, str(e)
        res = self.engine.run_build(
            item.slug,
            app,
            stop_at_checkpoint=False,
            env=self._claudo_env(),
            timeout=self.cfg.claudo_timeout,
        )
        self.store.write(item, "claudo-final.log", res.log[-30000:])
        self._add_claudo_cost(item, app)
        if res.ok:
            self._commit_leftovers(item, app)
        if res.outcome != "done":
            return False, f"Claudo did not complete after approval ({res.outcome}):\n{res.log[-2500:]}"
        item.claudo_cp = ""
        return True, "approved"

    @staticmethod
    def _sha(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _record_hash(self, item: WorkItem, name: str) -> None:
        item.approved_hashes[name] = self._sha(self.store.read(item, name))

    def _immutability_gate(self, item: WorkItem) -> GateResult:
        """spec.md and design.md must be byte-for-byte what a human approved. "Never edit the spec during a
        build" was only a prompt rule: an agent that weakens an eval in its own copy would pass every other
        gate. This makes the rule mechanical, for the store's copy and for the copy inside the app."""
        copies = self.app_dir(item) / "work" / item.slug
        problems = []
        for name in ("spec.md", "design.md"):
            approved = item.approved_hashes.get(name)
            if not approved:
                continue  # item approved before hashes were recorded: nothing to compare against
            if self._sha(self.store.read(item, name)) != approved:
                problems.append(
                    f"{name}: changed in work/{item.slug}/ after its approval; amend it through the review "
                    "checkpoint (reject, regenerate, approve again)"
                )
            copy = copies / name
            if not copy.is_file():
                problems.append(f"{name}: missing from the app (work/{item.slug}/{name})")
            elif self._sha(copy.read_text(encoding="utf-8")) != approved:
                problems.append(
                    f"{name}: modified inside the app during the build; agents never edit the contract"
                )
        detail = "\n".join(problems) or "spec.md and design.md are as approved"
        return GateResult("immutable", not problems, detail)

    def _trajectory_gate(self, item: WorkItem) -> GateResult:
        if self.engine is None or not item.claudo_cp:
            return GateResult("trajectory", True, "not applicable: this item was not built through Claudo")
        ok, out = self.engine.trajectory(self.app_dir(item), item.slug)
        return GateResult("trajectory", ok, out or "ok")

    def _do_gate(self, item: WorkItem) -> tuple[bool, str]:
        app = self.app_dir(item)
        results = run_gates(
            self.cfg.gates_for(item.maturity),
            app,
            radar=self.radar,
            maturity=item.maturity,
            exceptions=item.active_exceptions(self.today()),
            docs=[app / "work" / item.slug / "design.md"],
            commands=self.cfg.gate_commands,
            executor=self.executor,
            extra={
                "immutable": lambda: self._immutability_gate(item),
                "trajectory": lambda: self._trajectory_gate(item),
                "clean_tree": lambda: self._clean_tree_gate(item),
            },
        )
        self.store.write(item, "gate-report.md", format_report(results, item.maturity))
        failed = [r for r in results if not r.ok]
        if failed:
            item.stage = "build"  # the next build gets this detail as feedback
            detail = "\n\n".join(f"[{r.name}] {r.detail}" for r in failed)
            return False, "gates failed:\n" + detail[-3000:]
        self.judge_artifact(item, "build")
        return True, "gates passed: " + ", ".join(r.name for r in results)


def describe_step(stage: str) -> str:
    step = STEP_BY_NAME[stage]
    who = f" [{step.role}]" if step.role else ""
    return f"{stage}{who}: {step.summary}"
