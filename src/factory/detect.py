"""Find which technologies a project actually uses.

Sources, from strongest to weakest evidence:
  manifest  direct dependencies (pyproject.toml, requirements*.txt, package.json)
  image     Docker base images (Dockerfile FROM, compose `image:`)
  import    import statements in Python / JS / TS sources
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from factory.project import GIT_TIMEOUT, git_argv, git_env

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    ".pytest_cache",
    ".ruff_cache",
}

_DEP_NAME = re.compile(r"^\s*([A-Za-z0-9@][A-Za-z0-9._/@-]*)")
_PY_IMPORT = re.compile(r"^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import\b|import\s+([A-Za-z_][\w.]*))", re.M)
_JS_IMPORT = re.compile(r"""(?:\bfrom\s+|\bimport\s+|\brequire\(\s*)['"]([^'"]+)['"]""")
_FROM = re.compile(r"^\s*FROM\s+(?:--\S+\s+)*(\S+)", re.I | re.M)
_COMPOSE_IMAGE = re.compile(r"^\s*image:\s*['\"]?([^\s'\"#]+)", re.M)


@dataclass(frozen=True)
class Finding:
    name: str
    source: str  # path relative to the project root
    kind: str  # manifest | image | import | error (unparseable manifest) | unanalysed (unsupported ecosystem)


# Third-party code, even when a repository commits it: scanning it is slow and judges other people's
# dependencies, not the app (J-12). Tracked build/ and dist/ ARE scanned: that is the app's own output.
DEPENDENCY_DIRS = {"node_modules", ".venv", "venv", "vendor", "site-packages"}


def _git_files(root: Path, *flags: str) -> list[str] | None:
    p = subprocess.run(
        git_argv("ls-files", "-z", *flags),
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=git_env(),
        timeout=GIT_TIMEOUT,
    )
    return [n for n in p.stdout.split("\0") if n] if p.returncode == 0 else None


def iter_files(root: Path, *, include_dependencies: bool = False) -> Iterator[Path]:
    """The files a scan must judge. In a git project: everything git TRACKS (that is what gets published,
    whatever its folder is called) plus untracked files git would not ignore, minus junk folders. Tracked
    third-party folders are skipped unless `include_dependencies` (the secrets gate: they are published too).
    Otherwise: a plain walk."""
    if (root / ".git").exists():
        tracked = _git_files(root, "-c")
        untracked = _git_files(root, "-o", "--exclude-standard")
        if tracked is not None and untracked is not None:
            junk_free = [n for n in untracked if not any(part in SKIP_DIRS for part in Path(n).parts)]
            own = [
                n
                for n in tracked
                if include_dependencies or not any(part in DEPENDENCY_DIRS for part in Path(n).parts)
            ]
            for name in sorted(set(own) | set(junk_free)):
                if (root / name).is_file():
                    yield root / name
            return
    for p in sorted(root.rglob("*")):
        if p.is_file() and not any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            yield p


def _dep_name(spec: str) -> str | None:
    m = _DEP_NAME.match(spec)
    if not m:
        return None
    return m.group(1).split("[")[0]


def _image_name(ref: str) -> str:
    """'docker.io/library/python:3.12-slim' -> 'python'."""
    ref = ref.split("@")[0]
    last = ref.rsplit("/", 1)[-1]
    return last.split(":")[0]


def _pyproject(text: str) -> list[str]:
    data = tomllib.loads(text)
    project = data.get("project", {})
    specs = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        specs += group
    for group in data.get("dependency-groups", {}).values():
        specs += [s for s in group if isinstance(s, str)]
    names = [n for n in map(_dep_name, specs) if n]
    poetry = data.get("tool", {}).get("poetry", {})
    for table in (
        poetry.get("dependencies", {}),
        poetry.get("dev-dependencies", {}),
        *[g.get("dependencies", {}) for g in poetry.get("group", {}).values()],
    ):
        names += [k for k in table if k.lower() != "python"]
    return names


pyproject_dependencies = _pyproject  # public name: the names of every declared dependency


def _requirements(text: str) -> list[str]:
    lines = (ln.split("#")[0].strip() for ln in text.splitlines())
    return [n for n in (_dep_name(ln) for ln in lines if ln and not ln.startswith("-")) if n]


def _package_json(text: str) -> list[str]:
    data = json.loads(text)
    names: list[str] = []
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        names += list(data.get(key, {}))
    return names


def _js_module(spec: str) -> str | None:
    if spec.startswith((".", "/")):
        return None
    parts = spec.split("/")
    return "/".join(parts[:2]) if spec.startswith("@") else parts[0]


# Manifests of ecosystems the radar does not analyse: silently ignoring them would be a blind spot.
UNANALYSED_MANIFESTS = {
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "cargo.toml",
    "gemfile",
    "composer.json",
}


def _is_analysed_manifest(name: str) -> bool:
    return name in ("pyproject.toml", "package.json") or (
        name.startswith("requirements") and name.endswith(".txt")
    )


def scan_file(path: Path, rel: str) -> list[Finding]:
    name = path.name.lower()
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        # An unreadable manifest must not read as "no dependencies": that would pass the radar gate.
        return [Finding(rel, rel, "error")] if _is_analysed_manifest(name) else []
    if name in UNANALYSED_MANIFESTS:
        return [Finding(path.name, rel, "unanalysed")]
    try:
        if name == "pyproject.toml":
            return [Finding(n, rel, "manifest") for n in _pyproject(text)]
        if name.startswith("requirements") and name.endswith(".txt"):
            return [Finding(n, rel, "manifest") for n in _requirements(text)]
        if name == "package.json":
            return [Finding(n, rel, "manifest") for n in _package_json(text)]
    except (tomllib.TOMLDecodeError, json.JSONDecodeError, TypeError, AttributeError):
        return [Finding(rel, rel, "error")]  # the radar cannot see this manifest: reported, never ignored
    if name.startswith("dockerfile") or name.endswith(".dockerfile"):
        return [Finding(_image_name(m), rel, "image") for m in _FROM.findall(text) if m.lower() != "scratch"]
    if name.startswith(("docker-compose", "compose")) and name.endswith((".yml", ".yaml")):
        return [Finding(_image_name(m), rel, "image") for m in _COMPOSE_IMAGE.findall(text)]
    if name.endswith(".py"):
        mods = {(a or b).split(".")[0] for a, b in _PY_IMPORT.findall(text)}
        return [Finding(m, rel, "import") for m in sorted(mods)]
    if name.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")):
        mods = {m for m in map(_js_module, _JS_IMPORT.findall(text)) if m}
        return [Finding(m, rel, "import") for m in sorted(mods)]
    return []


def scan_project(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in iter_files(root):
        findings += scan_file(path, path.relative_to(root).as_posix())
    return findings
