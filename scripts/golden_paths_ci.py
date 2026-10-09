"""Scaffold every golden path through the factory, then prove the result works (audit A9).

For each template: an offline factory item scaffolds it (the real placeholders, the real first commit), then
`uv lock`, `uv sync --locked`, ruff, pytest, the frontend's npm ci/test/build when there is one, and with
`--docker` a `docker build` of the image. Run by the root CI; locally:
`uv run python scripts/golden_paths_ci.py`. Exit code 0 when every template passes.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from factory.config import load_config  # noqa: E402
from factory.foreman import Foreman  # noqa: E402
from factory.radar import load_radar  # noqa: E402
from factory.workitem import Store  # noqa: E402

# An idea per template: the factory must pick that template for it.
IDEAS = {
    "python-fastapi": ("Order api", "An HTTP API that lists orders"),
    "python-worker": ("Order events", "Consume order events from a queue"),
    "fullstack-react": ("Visitor log", "A page listing visitors"),
}


def scaffold(root: Path, golden: str) -> Path:
    cfg = load_config(root)
    foreman = Foreman(cfg, load_radar(cfg.radar_path), Store(cfg.work_dir), runner=None)
    title, idea = IDEAS[golden]
    item = foreman.run(foreman.intake(title, idea, "poc"))
    item = foreman.approve(foreman.approve(item, "business"), "owner")
    if item.golden_path != golden:
        raise SystemExit(f"{golden}: the factory picked {item.golden_path!r} for {idea!r}")
    return foreman.app_dir(item)


def run(argv: list[str], cwd: Path) -> None:
    print(f"$ {' '.join(argv)}", flush=True)
    exe = shutil.which(argv[0]) or argv[0]  # npm is npm.cmd on Windows
    subprocess.run([exe, *argv[1:]], cwd=cwd, check=True)


def check(app: Path, golden: str, docker: bool) -> None:
    run(["uv", "lock", "--quiet"], app)
    run(["uv", "sync", "--locked", "--quiet"], app)
    run(["uv", "run", "--quiet", "ruff", "check", "."], app)
    run(["uv", "run", "--quiet", "pytest", "-q"], app)
    if (app / "web" / "package.json").is_file():
        for argv in (["npm", "ci", "--no-audit", "--no-fund"], ["npm", "test"], ["npm", "run", "build"]):
            run(argv, app / "web")
    if docker:
        run(["docker", "build", "--quiet", "-t", f"golden-{golden}:ci", "."], app)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--docker", action="store_true", help="also docker build each image")
    parser.add_argument("only", nargs="*", help="templates to check (default: all)")
    args = parser.parse_args(argv)
    failed = []
    for golden in args.only or list(IDEAS):
        with tempfile.TemporaryDirectory(prefix=f"gp-{golden}-") as tmp:
            root = Path(tmp)
            for name in ("factory.toml", "radar.toml"):
                shutil.copy2(REPO / name, root / name)
            shutil.copytree(REPO / "golden_paths", root / "golden_paths")
            print(f"=== {golden}", flush=True)
            try:
                check(scaffold(root, golden), golden, args.docker)
            except subprocess.CalledProcessError as e:
                print(f"FAIL {golden}: {e}", flush=True)
                failed.append(golden)
    print("all golden paths pass" if not failed else f"FAILED: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
