"""`factory demo`: a self-checking, offline walkthrough of the governance story (about 5 seconds).

It runs in a throwaway copy of the factory, so it never touches your work/ or apps/. Every
step asserts the state it narrates: if the demo prints "DEMO OK", the story really happened.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from factory.config import load_config
from factory.foreman import Foreman
from factory.guard import check_project
from factory.radar import load_radar
from factory.workitem import Store

Say = Callable[[str], None]


class DemoError(AssertionError):
    pass


def expect(cond: bool, message: str) -> None:
    if not cond:
        raise DemoError(message)


def _foreman(root: Path, with_tests: bool) -> Foreman:
    cfg = load_config(root)
    if not with_tests:  # radar + secrets gates only: no uv, no network, no billing
        cfg.gates = {m: [g for g in gs if g in ("radar", "secrets")] for m, gs in cfg.gates.items()}
    # Without the tests gate the demo needs no lockfile (no dependencies gate either): it stays offline.
    locker = None if with_tests else (lambda app: (0, "the demo does not lock"))
    return Foreman(cfg, load_radar(cfg.radar_path), Store(cfg.work_dir), runner=None, locker=locker)


def run_demo(source: Path, say: Say = print, with_tests: bool = False) -> None:
    with tempfile.TemporaryDirectory(prefix="factory-demo-") as tmp:
        root = Path(tmp)
        src_cfg = load_config(source)  # the radar and golden paths factory.toml names, wherever they are
        shutil.copy2(source / "factory.toml", root / "factory.toml")
        radar = root / src_cfg.radar_path.relative_to(source)
        radar.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_cfg.radar_path, radar)
        shutil.copytree(src_cfg.golden_paths_dir, root / src_cfg.golden_paths_dir.relative_to(source))
        f = _foreman(root, with_tests)

        def step(title: str) -> None:
            say(f"\n=== {title}")

        step("1. IT owns the rules: the tech radar")
        rings = {
            r: [t.name for t in f.radar.techs if t.ring == r] for r in ("adopt", "trial", "assess", "hold")
        }
        for ring, names in rings.items():
            say(f"  {ring:<6} {len(names):>2}  {', '.join(names[:6])}{' ...' if len(names) > 6 else ''}")

        step("2. Business submits an idea (and asks for a forbidden technology)")
        idea = "A page where support agents record customer callbacks and track history. Store in MongoDB."
        item = f.intake("Callback log", idea, maturity="poc", requester="alice@business")
        say(f'  "{idea}"')
        item = f.run(item)
        for note in item.notes:
            say(f"  triage: {note}")
        expect(any("MongoDB" in n and "PostgreSQL" in n for n in item.notes), "triage did not catch MongoDB")
        expect(item.stage == "spec_review" and item.status == "waiting", "expected to wait for business")

        step("3. Business approves the spec; the stack is COMPILED from the radar")
        item = f.approve(item, "business", "alice")
        design = f.store.read(item, "design.md")
        stack_lines = [ln for ln in design.splitlines() if ln.startswith("| ") and "adopt" in ln]
        for ln in stack_lines:
            say(f"  {ln}")
        expect("PostgreSQL" in design and item.stage == "plan_review", "design should use PostgreSQL")

        step("4. Owner approves the plan: scaffold from the IT golden path, then gates run")
        item = f.approve(item, "owner", "antoine")
        expect(item.stage == "ship_review", f"build/gates failed: {item.feedback[:200]}")
        gate_event = next(h for h in reversed(item.history) if h["detail"].startswith("gates passed"))
        say(f"  {gate_event['detail']}")
        app = f.app_dir(item)

        step("5. An agent sneaks in a forbidden dependency (Flask is on hold)")
        pyproject = app / "pyproject.toml"
        original = pyproject.read_bytes()  # restored byte for byte: the gated commit must stay clean
        pyproject.write_bytes(original.replace(b'"pydantic>=2.7",', b'"pydantic>=2.7",\n    "flask",'))
        report = check_project(app, f.radar, "poc")
        for v in report.blocking():
            say(f"  BLOCKED {v.describe()}")
        expect([v.key for v in report.blocking()] == ["flask"], "radar guard missed the Flask dependency")
        pyproject.write_bytes(original)
        expect(check_project(app, f.radar, "poc").ok(), "guard should pass once Flask is removed")
        say("  removed -> radar guard passes again")

        step("6. IT merges: the factory never merges by itself")
        item = f.approve(item, "it", "bob")
        expect(item.status == "shipped", "item should be shipped")
        say(f"  shipped -> approvals: {', '.join(a['role'] for a in item.approvals)}")

        step("7. Same factory, higher stakes: an MVP on a 'trial' technology needs IT")
        item2 = f.intake("Orders API", "An orders API built with Django", maturity="mvp")
        item2 = f.approve(f.run(item2), "business", "alice")
        expect(item2.stage == "design_review", "MVP + trial tech should wait for IT design review")
        say(f"  {item2.slug}: waiting on IT -> Django is 'trial', allowed at MVP only with approval")
        item2 = f.approve(item2, "it", "bob", note="Django fine for this team")
        say(f"  IT approved, exception recorded: {item2.it_exceptions}")
        expect(item2.it_exceptions == ["django"], "exception not recorded")

        say("\nDEMO OK: radar enforced, exception path works, merge stayed human.")
