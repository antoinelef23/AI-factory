"""`factory` command line: the business, IT and owner entry points."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from factory.agents import AgentError, get_runner
from factory.config import ConfigError, find_root, load_config
from factory.foreman import FactoryError, Foreman, describe_step
from factory.guard import check_project
from factory.radar import MATURITIES, POLICY, RINGS, RadarError, load_radar
from factory.workitem import ROLES, Store, WorkItem


def _foreman(args: argparse.Namespace, runner_name: str | None = None) -> Foreman:
    root = Path(args.root).resolve() if args.root else find_root()
    cfg = load_config(root)
    radar = load_radar(cfg.radar_path)
    return Foreman(cfg, radar, Store(cfg.work_dir), runner=get_runner(runner_name or cfg.runner))


def _print_item(f: Foreman, item: WorkItem, verbose: bool = False) -> None:
    print(f"{item.slug}  [{item.maturity}]  {item.title}")
    print(f"  stage : {describe_step(item.stage)}")
    print(f"  status: {item.status}" + (f"   cost: ${item.cost_usd:.2f}" if item.cost_usd else ""))
    for note in item.notes:
        print(f"  note  : {note}")
    if item.it_exceptions:
        print(f"  IT exceptions: {', '.join(item.it_exceptions)}")
    for kind, j in item.judgements.items():
        print(
            f"  judge {kind}: {j['verdict']} ({j['average']}/5, advisory)  work/{item.slug}/judge-{kind}.md"
        )
    if item.feedback and item.status in ("blocked", "active"):
        print("  feedback:")
        for line in item.feedback.splitlines()[:12]:
            print(f"    {line}")
    if item.status == "waiting":
        role = item.step.role
        print(
            f"  next  : factory approve {item.slug} --as {role}   |   factory reject {item.slug} --as {role} "
            f'--reason "..."'
        )
    elif item.status == "blocked":
        print(f"  next  : fix or explain, then `factory run {item.slug}`")
        print(f"  report: work/{item.slug}/gate-report.md")
    elif item.status == "shipped":
        print(f"  app   : {f.app_dir(item)}")
    if verbose:
        print("  history:")
        for h in item.history:
            print(f"    {h['at']}  {h['stage']:<13} {h['event']:<9} {h['detail'][:110]}")
    print(f"  files : {f.store.dir(item.slug)}")


def cmd_intake(args: argparse.Namespace) -> int:
    f = _foreman(args, "offline")
    item = f.intake(args.title, args.idea, args.maturity, args.requester)
    print(f"Work item created: {item.slug}")
    print(f"Next: factory run {item.slug}   (add --runner claude to use Claude Code)")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    f = _foreman(args, args.runner)
    item = f.run(f.store.load(args.slug))
    _print_item(f, item)
    return 1 if item.status == "blocked" else 0


def cmd_approve(args: argparse.Namespace) -> int:
    f = _foreman(args, args.runner)
    item = f.approve(f.store.load(args.slug), args.role, args.by, args.note)
    _print_item(f, item)
    return 1 if item.status == "blocked" else 0


def cmd_reject(args: argparse.Namespace) -> int:
    f = _foreman(args, "offline")
    item = f.reject(f.store.load(args.slug), args.role, args.reason, args.by)
    _print_item(f, item)
    return 0


def cmd_allow(args: argparse.Namespace) -> int:
    f = _foreman(args, "offline")
    item = f.allow(f.store.load(args.slug), args.tech, args.role, args.by)
    print(f"IT exceptions for {item.slug}: {', '.join(item.it_exceptions)}")
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    f = _foreman(args, args.runner)
    item = f.promote(f.store.load(args.slug), args.to, args.role, args.by)
    _print_item(f, item)
    return 1 if item.status == "blocked" else 0


def cmd_show(args: argparse.Namespace) -> int:
    f = _foreman(args, "offline")
    _print_item(f, f.store.load(args.slug), verbose=True)
    return 0


def cmd_board(args: argparse.Namespace) -> int:
    f = _foreman(args, "offline")
    items = f.store.all()
    if not items:
        print('No work items yet. Start with: factory intake "Title" --idea "..." --maturity poc')
        return 0
    print(f"{'ITEM':<34} {'MAT':<5} {'STAGE':<14} {'STATUS':<8} WAITING ON")
    for i in items:
        waiting = i.step.role if i.status == "waiting" else ("fix" if i.status == "blocked" else "-")
        print(f"{i.slug:<34} {i.maturity:<5} {i.stage:<14} {i.status:<8} {waiting}")
    return 0


def cmd_radar(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve() if args.root else find_root()
    radar = load_radar(load_config(root).radar_path)
    print(f"Tech radar: {radar.company} {radar.version}")
    for ring in RINGS:
        techs = [t for t in radar.techs if t.ring == ring]
        allowed = ", ".join(f"{m}={POLICY[ring][m]}" for m in MATURITIES)
        print(f"\n{ring.upper()}  ({allowed})")
        for t in techs:
            extra = f" -> use {t.replaced_by}" if t.replaced_by else ""
            gp = f"  [golden path: {t.golden_path}]" if t.golden_path else ""
            print(f"  {t.id:<16} {t.category:<10} {t.name}{extra}{gp}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve() if args.root else find_root()
    radar = load_radar(load_config(root).radar_path)
    target = Path(args.path).resolve()
    docs = [target / d for d in args.doc] if args.doc else []
    report = check_project(target, radar, args.maturity, docs=docs)
    exceptions = set(filter(None, (args.allow or "").split(",")))
    blocking = report.blocking(exceptions)
    print(f"radar check of {target} at maturity {args.maturity} ({radar.company} {radar.version})")
    print(f"  allowed: {', '.join(report.allowed) or '-'}")
    for v in report.violations:
        mark = "OK (IT exception)" if v not in blocking else "FAIL"
        print(f"  {mark:<17} {v.describe()}")
    print("PASS" if not blocking else f"FAIL: {len(blocking)} blocking violation(s)")
    return 0 if not blocking else 1


def cmd_export(args: argparse.Namespace) -> int:
    """Hand a work item's triplet to a Claudo project (work/<slug>/), for Claudo's orchestrator."""
    f = _foreman(args, "offline")
    item = f.store.load(args.slug)
    dest = Path(args.claudo).resolve() / "work" / item.slug
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in ("spec.md", "design.md", "tasks.md"):
        src = f.store.dir(item.slug) / name
        if src.is_file():
            shutil.copy2(src, dest / name)
            copied.append(name)
    print(f"Exported {', '.join(copied) or 'nothing'} to {dest}")
    project = Path(args.claudo).resolve().as_posix()
    print(f"Run with Claudo (Linux/WSL): just run-project {project} work/{item.slug}")
    return 0


def cmd_judge(args: argparse.Namespace) -> int:
    f = _foreman(args, "claude")  # judging needs a real model
    item = f.store.load(args.slug)
    report = f.judge_artifact(item, args.kind, force=True)
    if report is None:
        print(f"nothing to judge: no {args.kind} artifact yet for {item.slug}", file=sys.stderr)
        return 2
    f.store.save(item)
    print(report.markdown())
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from factory.demo import DemoError, run_demo

    root = Path(args.root).resolve() if args.root else find_root()
    try:
        run_demo(root, with_tests=args.with_tests)
    except DemoError as e:
        print(f"DEMO FAILED: {e}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="factory", description="AI software factory governed by the IT tech radar"
    )
    p.add_argument("--root", help="factory root (default: nearest factory.toml, or AI_FACTORY_ROOT)")
    sub = p.add_subparsers(dest="command", required=True)

    def runner_opt(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--runner", choices=["offline", "claude"], help="override factory.toml [agent] runner"
        )

    def role_opt(sp: argparse.ArgumentParser, choices=ROLES) -> None:
        sp.add_argument("--as", dest="role", required=True, choices=choices, help="who decides")
        sp.add_argument("--by", default="", help="name of the person deciding")

    sp = sub.add_parser("intake", help="business: submit an idea")
    sp.add_argument("title")
    sp.add_argument("--idea", required=True, help="what the business wants, in plain words")
    sp.add_argument("--maturity", default="poc", choices=MATURITIES)
    sp.add_argument("--requester", default="business")
    sp.set_defaults(func=cmd_intake)

    sp = sub.add_parser("run", help="run automatic stages until the next checkpoint")
    sp.add_argument("slug")
    runner_opt(sp)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("approve", help="approve the checkpoint an item is waiting on")
    sp.add_argument("slug")
    role_opt(sp)
    sp.add_argument("--note", default="")
    runner_opt(sp)
    sp.set_defaults(func=cmd_approve)

    sp = sub.add_parser("reject", help="reject a checkpoint, with a reason fed to the next attempt")
    sp.add_argument("slug")
    role_opt(sp)
    sp.add_argument("--reason", required=True)
    sp.set_defaults(func=cmd_reject)

    sp = sub.add_parser("allow", help="IT: grant a tech radar exception to one item")
    sp.add_argument("slug")
    sp.add_argument("tech")
    role_opt(sp, ("it",))
    sp.set_defaults(func=cmd_allow)

    sp = sub.add_parser("promote", help="IT: move a shipped app up the maturity ladder")
    sp.add_argument("slug")
    sp.add_argument("--to", required=True, choices=MATURITIES)
    role_opt(sp, ("it",))
    runner_opt(sp)
    sp.set_defaults(func=cmd_promote)

    sp = sub.add_parser("show", help="item details and history")
    sp.add_argument("slug")
    sp.set_defaults(func=cmd_show)

    sub.add_parser("board", help="all work items").set_defaults(func=cmd_board)
    sub.add_parser("radar", help="show the company tech radar").set_defaults(func=cmd_radar)

    sp = sub.add_parser("judge", help="LLM judge on an artifact (advisory, billed: uses the judge model)")
    sp.add_argument("slug")
    sp.add_argument("--kind", choices=["spec", "plan", "build"], default="spec")
    sp.set_defaults(func=cmd_judge)

    sp = sub.add_parser("demo", help="offline, self-checking walkthrough in a temp folder (about 5 s)")
    sp.add_argument(
        "--with-tests", action="store_true", help="also run the app's pytest gate (needs uv + network)"
    )
    sp.set_defaults(func=cmd_demo)

    sp = sub.add_parser("check", help="IT: radar-check any project folder")
    sp.add_argument("path")
    sp.add_argument("--maturity", default="prod", choices=MATURITIES)
    sp.add_argument("--doc", action="append", help="design doc to text-scan (relative to path)")
    sp.add_argument("--allow", help="comma-separated IT exceptions")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("export", help="copy an item's spec/design/tasks into a Claudo project")
    sp.add_argument("slug")
    sp.add_argument("--claudo", required=True, help="path of the Claudo project")
    sp.set_defaults(func=cmd_export)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, RadarError, FactoryError, AgentError, KeyError) as e:
        msg = e.args[0] if isinstance(e, KeyError) and e.args else e
        print(f"error: {msg}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
