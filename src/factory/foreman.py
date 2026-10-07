"""The foreman: moves work items through the stages, stops at human checkpoints.

Automatic stages run back to back; a checkpoint parks the item (status `waiting`) until
the right role approves or rejects it. A failed stage parks it as `blocked` with the
reason in `feedback`, which the next attempt receives.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from factory.agents import BUILD_TOOLS, READ_ONLY_TOOLS, ClaudeRunner, strip_fences
from factory.config import Config
from factory.design import choose_stack, detect_capabilities, render_design
from factory.gates import Executor, format_report, run_gates, shell_executor
from factory.guard import check_project
from factory.judge import judge
from factory.radar import BLOCK, MATURITIES, Radar, verdict
from factory.templates import (
    build_prompt,
    idea_md,
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
    ) -> None:
        self.cfg = cfg
        self.radar = radar
        self.store = store
        self.runner = runner
        self.executor = executor

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

    # ------------------------------------------------------------------ driving
    def run(self, item: WorkItem, max_steps: int = 20) -> WorkItem:
        """Run automatic stages until a checkpoint, a failure, or shipped."""
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
            item.it_exceptions = sorted(set(item.it_exceptions) | {v.key for v in pending})
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
        item.log("rejected", f"{role} {by}".strip() + f": {reason}")
        item.feedback = reason
        item.stage = back
        item.status = "active"
        self.store.save(item)
        return item

    def allow(self, item: WorkItem, tech: str, role: str, by: str = "") -> WorkItem:
        """IT grants a radar exception for this item (e.g. a trial tech at MVP)."""
        if role != "it":
            raise FactoryError("only IT can grant a tech radar exception")
        known = self.radar.get(tech) or self.radar.find(tech)
        key = known.id if known else tech
        if known and known.ring == "hold":
            raise FactoryError(
                f"{known.name} is on hold: change the radar (radar.toml) instead of an exception"
            )
        if key not in item.it_exceptions:
            item.it_exceptions.append(key)
        item.log("exception", f"IT {by} allowed {key}".replace("  ", " "))
        self.store.save(item)
        return item

    def promote(self, item: WorkItem, to: str, role: str, by: str = "") -> WorkItem:
        """POV -> POC -> MVP -> prod: back through design with the stricter rules."""
        if role != "it":
            raise FactoryError("only IT can promote an app to a higher maturity")
        if item.stage != "shipped":
            raise FactoryError("only a shipped item can be promoted")
        if MATURITIES.index(to) <= MATURITIES.index(item.maturity):
            raise FactoryError(f"cannot promote from {item.maturity} to {to}")
        item.log("promoted", f"{item.maturity} -> {to} by IT {by}".strip())
        item.maturity = to
        item.it_exceptions = []  # exceptions were granted for the previous rung
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
        if self.runner is None:
            text = offline_spec(item)
        else:
            ok, text = self._ask_agent(item, spec_prompt(item, item.feedback), "spec")
            if not ok:
                return False, text
        self.store.write(item, "spec.md", text)
        self.judge_artifact(item, "spec")
        return True, "spec.md written" + (" (offline template)" if self.runner is None else "")

    def _design_report(self, item: WorkItem):
        d = self.store.dir(item.slug)
        return check_project(d, self.radar, item.maturity, docs=[d / "design.md"])

    def _do_design(self, item: WorkItem) -> tuple[bool, str]:
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

    def _do_plan(self, item: WorkItem) -> tuple[bool, str]:
        if self.runner is None:
            text = offline_tasks(item, self._golden_path(item))
        else:
            ok, text = self._ask_agent(item, plan_prompt(item, item.feedback), "plan")
            if not ok:
                return False, text
        self.store.write(item, "tasks.md", text)
        self.judge_artifact(item, "plan")
        return True, "tasks.md written" + (" (offline template)" if self.runner is None else "")

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
        return self.cfg.apps_dir / item.slug

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
                dest.write_text(text, encoding="utf-8")
            else:
                shutil.copy2(path, dest)
        return f"scaffolded from golden_paths/{gp}"

    def _do_build(self, item: WorkItem) -> tuple[bool, str]:
        app = self.app_dir(item)
        detail = self._scaffold(item, app)
        # The triplet travels with the app (Claudo layout: work/<feature>/).
        triplet = app / "work" / item.slug
        triplet.mkdir(parents=True, exist_ok=True)
        for name in ("idea.md", "spec.md", "design.md", "tasks.md"):
            text = self.store.read(item, name)
            if text:
                (triplet / name).write_text(text, encoding="utf-8")
        if self.runner is None:
            return True, f"{detail}; offline build (scaffold only, no agent)"
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

    def _do_gate(self, item: WorkItem) -> tuple[bool, str]:
        app = self.app_dir(item)
        results = run_gates(
            self.cfg.gates_for(item.maturity),
            app,
            radar=self.radar,
            maturity=item.maturity,
            exceptions=set(item.it_exceptions),
            docs=[app / "work" / item.slug / "design.md"],
            commands=self.cfg.gate_commands,
            executor=self.executor,
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
