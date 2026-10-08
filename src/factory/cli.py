"""`factory` command line: the business, IT and owner entry points."""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import date
from pathlib import Path

from factory.agents import AgentError, get_runner
from factory.claudo import ClaudoEngine, EngineError, discover
from factory.config import ConfigError, find_root, load_config
from factory.delivery import GhCli
from factory.drift import radar_diff, render_diff, scan_drift
from factory.foreman import FactoryError, Foreman, describe_step
from factory.guard import check_project
from factory.importer import import_radar
from factory.radar import MATURITIES, POLICY, RINGS, RadarError, load_radar
from factory.workitem import ROLES, Store, WorkItem


def _foreman(args: argparse.Namespace, runner_name: str | None = None) -> Foreman:
    root = Path(args.root).resolve() if args.root else find_root()
    cfg = load_config(root)
    radar = load_radar(cfg.radar_path)
    home = discover(cfg.claudo_home, root)  # None = no Claudo around: plans are simply not linted
    engine = ClaudoEngine(home) if home else None
    host = GhCli() if cfg.delivery_provider == "github" else None
    return Foreman(
        cfg,
        radar,
        Store(cfg.work_dir),
        runner=get_runner(runner_name or cfg.runner),
        engine=engine,
        host=host,
    )


def _print_item(f: Foreman, item: WorkItem, verbose: bool = False) -> None:
    print(f"{item.slug}  [{item.maturity}]  {item.title}")
    if item.repo_url:
        print(f"  repo  : {item.repo} (private)")
    if item.pr_url:
        print(f"  pr    : {item.pr_url} ({item.pr_state or 'OPEN'})")
    if item.kind != "app":
        state = "merged" if item.merged else ("abandoned" if item.status == "abandoned" else "not merged yet")
        print(f"  change: {item.kind} of {item.target}, branch factory/{item.slug} ({state})")
    print(f"  stage : {describe_step(item.stage)}")
    print(f"  status: {item.status}" + (f"   cost: ${item.cost_usd:.2f}" if item.cost_usd else ""))
    for note in item.notes:
        print(f"  note  : {note}")
    if item.it_exceptions:
        today = date.today()
        shown = [
            f"{k} (until {item.expiry_of(k)})" if item.expiry_of(k) else f"{k} (no expiry)"
            for k in item.it_exceptions
        ]
        print(f"  IT exceptions: {', '.join(shown)}")
        lapsed = item.expired_exceptions(today)
        if lapsed:
            print(f"  EXPIRED exceptions: {', '.join(lapsed)}: renew with `factory allow`")
    if item.claudo_review:
        r = item.claudo_review
        print(f"  Claudo reviewer {r['cp']}: {r['verdict']}  apps/{item.slug}/{r['report']}")
    for ack in item.ship_acks:
        print(f"  needs IT: {ack['kind']}: {ack['detail']}")
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
    elif item.status == "shipped" and item.pr_url and not item.merged:
        print(f"  next  : merge the pull request on the host, then `factory sync {item.slug}`")
    elif item.status == "shipped" and not item.repo_url and f.host is not None and not item.merged:
        print(f"  next  : factory publish {item.slug} --as it   (private repository / pull request)")
    elif item.status == "shipped" and item.kind != "app" and not item.merged:
        print(f"  next  : factory merge {item.slug} --as it   (IT merges: the factory never does)")
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


def cmd_change(args: argparse.Namespace) -> int:
    f = _foreman(args, "offline")
    item = f.intake_change(args.target, args.title, args.idea, args.kind, args.requester)
    print(f"Change created: {item.slug}  ({item.kind} of {item.target}, maturity {item.maturity})")
    print(f"Next: factory run {item.slug}   (add --runner claude to use Claude Code)")
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    f = _foreman(args, "offline")
    item = f.merge(f.store.load(args.slug), args.role, args.by)
    print(f"Merged {item.slug} into {item.base_branch} of {item.target} (fast-forward).")
    print("The app folder now shows the delivered state again; `factory drift` will judge it.")
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    f = _foreman(args, "offline")
    item = f.publish(f.store.load(args.slug), args.role, args.by)
    if item.kind == "app":
        print(f"Published {item.slug}: private repository {item.repo}")
        print(f"  {item.repo_url}")
    else:
        print(f"Pull request for {item.slug}: {item.pr_url}")
        print(f"The factory never merges: merge it on the host, then `factory sync {item.slug}`.")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    f = _foreman(args, "offline")
    item = f.sync(f.store.load(args.slug))
    if item.merged:
        print(
            f"{item.slug}: the pull request is merged; {item.target} is now at the merged {item.base_branch}."
        )
        print("`factory drift` will judge it.")
    else:
        print(f"{item.slug}: pull request {item.pr_state} ({item.pr_url})")
    return 0


def cmd_abandon(args: argparse.Namespace) -> int:
    f = _foreman(args, "offline")
    item = f.abandon(f.store.load(args.slug), args.role, args.reason, args.by)
    print(f"Abandoned {item.slug}: {item.target} is free for another change.")
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
    try:
        expires = date.fromisoformat(args.expires) if args.expires else None
    except ValueError:
        raise FactoryError(f"--expires must be a date YYYY-MM-DD, got {args.expires!r}") from None
    item = f.allow(f.store.load(args.slug), args.tech, args.role, args.by, expires, args.reason)
    for key in item.it_exceptions:
        print(f"  {key}: until {item.expiry_of(key) or 'no expiry'}")
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


def cmd_radar_import(args: argparse.Namespace) -> int:
    """Convert a company radar (CSV/JSON export) to radar.toml. Never overwrites IT's radar by accident."""
    root = Path(args.root).resolve() if args.root else find_root()
    source = Path(args.source)
    out = Path(args.out) if args.out else root / "radar.imported.toml"
    result = import_radar(
        source.read_text(encoding="utf-8"), source=source.name, company=args.company, version=args.version
    )
    for problem in result.problems:
        print(f"error: {problem}", file=sys.stderr)
    if not result.ok:
        return 2
    if out.exists() and not args.force:
        raise FactoryError(f"{out} exists: choose another --out, or pass --force to overwrite it")
    out.write_text(result.toml, encoding="utf-8", newline="\n")
    load_radar(out)  # the file we wrote must be one the factory accepts
    print(f"Imported {sum(result.counts.values())} technologies into {out}")
    print("  " + ", ".join(f"{ring}: {n}" for ring, n in result.counts.items()))
    for warning in result.warnings:
        print(f"  warning: {warning}")
    return 0


def cmd_radar_diff(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve() if args.root else find_root()
    old = load_radar(Path(args.old))
    new = load_radar(Path(args.new) if args.new else load_config(root).radar_path)
    print(render_diff(radar_diff(old, new), old, new))
    return 0


def cmd_drift(args: argparse.Namespace) -> int:
    """Re-check every shipped app against the CURRENT radar. Exit 1 when any app drifted (usable in CI)."""
    f = _foreman(args, "offline")
    items = f.store.all()
    flying = {i.target for i in items if i.change_open}
    drifted, clean = scan_drift(items, f.radar, f.cfg.apps_dir, in_flight=flying)
    print(f"Radar {f.radar.company} {f.radar.version}: {len(clean)} compliant, {len(drifted)} drifted")
    if flying:
        names = ", ".join(sorted(flying))
        print(f"  {len(flying)} app(s) with a change in flight, not judged until merged: {names}")
    # A migration exists because an app violated the radar: until it is merged the violation is still there,
    # so a scheduled drift check must keep failing instead of going green behind a pending change.
    pending = [i for i in items if i.change_open and i.kind == "migration"]
    for m in pending:
        print(f"  migration pending: {m.slug} (for {m.target}), stage {m.stage}, {m.status}")
    for d in drifted:
        print()
        print(f"  {d.item.slug} [{d.item.maturity}]")
        for v in d.violations:
            print(f"    {v.describe()}")
        if args.open:
            opened = f.open_migration(d)
            print(f"    -> migration item: {opened.slug}" if opened else "    -> migration already tracked")
    return 1 if drifted or pending else 0


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

    sp = sub.add_parser(
        "change", help="business: a feature, bug fix or migration for an EXISTING shipped app"
    )
    sp.add_argument("target", help="slug of the shipped app")
    sp.add_argument("title")
    sp.add_argument("--idea", required=True, help="what should change, in plain words")
    sp.add_argument("--kind", default="feature", choices=["feature", "bug", "migration"])
    sp.add_argument("--requester", default="business")
    sp.set_defaults(func=cmd_change)

    sp = sub.add_parser("merge", help="IT: merge an approved change into the app (fast-forward only)")
    sp.add_argument("slug")
    role_opt(sp, ("it",))
    sp.set_defaults(func=cmd_merge)

    sp = sub.add_parser("publish", help="IT: push a shipped app to its private repo / open a pull request")
    sp.add_argument("slug")
    role_opt(sp, ("it",))
    sp.set_defaults(func=cmd_publish)

    sp = sub.add_parser("sync", help="after the pull request was merged on the host, update the local app")
    sp.add_argument("slug")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("abandon", help="IT/owner: drop a change nobody wants, freeing the app")
    sp.add_argument("slug")
    role_opt(sp, ("it", "owner"))
    sp.add_argument("--reason", required=True)
    sp.set_defaults(func=cmd_abandon)

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
    sp.add_argument("--reason", required=True, help="why this exception is justified (kept with the item)")
    sp.add_argument(
        "--expires", help="last day it is valid, YYYY-MM-DD (default: the policy's exception_days)"
    )
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

    sp = sub.add_parser(
        "radar-import", help="IT: convert a CSV/JSON radar export (Thoughtworks BYOR style) to radar.toml"
    )
    sp.add_argument("source", help="the export (.csv or .json); needs a name and a ring column")
    sp.add_argument("--out", help="where to write (default: radar.imported.toml next to factory.toml)")
    sp.add_argument("--company", default="", help="company name recorded in the radar")
    sp.add_argument("--version", default="", help="radar version recorded in the radar, e.g. 2026.10")
    sp.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    sp.set_defaults(func=cmd_radar_import)

    sp = sub.add_parser("radar-diff", help="IT: what changed between two radars, and what it newly forbids")
    sp.add_argument("old", help="the previous radar.toml (e.g. from git show HEAD~1:radar.toml)")
    sp.add_argument("new", nargs="?", help="the new radar (default: the factory's current radar.toml)")
    sp.set_defaults(func=cmd_radar_diff)

    sp = sub.add_parser(
        "drift", help="IT: which shipped apps no longer comply with the current radar (exit 1 if any)"
    )
    sp.add_argument("--open", action="store_true", help="also open a tracked migration item per drifted app")
    sp.set_defaults(func=cmd_drift)

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


def _utf8_console() -> None:
    """Idea text, judge reports and radar notes contain non-ASCII ('€', '≤'); the Windows console
    codec (cp1252) would crash on them mid-run, after a billed call. Never fail on output."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass  # captured / replaced stream (tests): nothing to do


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, RadarError, FactoryError, AgentError, EngineError, KeyError) as e:
        msg = e.args[0] if isinstance(e, KeyError) and e.args else e
        print(f"error: {msg}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
