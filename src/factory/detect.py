"""Find which technologies a project actually uses.

Sources, from strongest to weakest evidence:
  manifest  direct dependencies (pyproject.toml, requirements*.txt, package.json)
  image     Docker base images (Dockerfile FROM, compose `image:`)
  import    import statements in Python / JS / TS sources
"""

from __future__ import annotations

import ast
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
_FROM = re.compile(r"^\s*FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?", re.I | re.M)
_ARG = re.compile(r"^\s*ARG\s+([A-Za-z_]\w*)(?:=(\S+))?", re.I | re.M)
_COPY_FROM = re.compile(r"^\s*COPY\s+(?:--\S+\s+)*?--from=(\S+)", re.I | re.M)
_ARG_REF = re.compile(r"\$\{?([A-Za-z_]\w*)\}?")
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
    tool = data.get("tool", {})
    specs += list(tool.get("uv", {}).get("dev-dependencies", []))  # the pre-PEP 735 uv form (audit A102)
    for group in tool.get("pdm", {}).get("dev-dependencies", {}).values():
        specs += list(group)
    names = [n for n in map(_dep_name, specs) if n]
    poetry = tool.get("poetry", {})
    for table in (
        poetry.get("dependencies", {}),
        poetry.get("dev-dependencies", {}),
        *[g.get("dependencies", {}) for g in poetry.get("group", {}).values()],
    ):
        names += [k for k in table if k.lower() != "python"]
    return names


pyproject_dependencies = _pyproject  # public name: the names of every declared dependency


def _requirements(text: str) -> tuple[list[str], list[str]]:
    """(dependency names, lines the radar cannot resolve to a name). `-r`/`-c` name other files (scanned on
    their own); `-e`/`--editable` installs a path or VCS URL: named by its #egg= when it has one, otherwise
    a blind spot that is reported, never dropped (audit A20)."""
    names, blind = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-e ", "--editable")):
            egg = re.search(r"[#&]egg=([A-Za-z0-9._-]+)", line)
            (names if egg else blind).append(egg.group(1) if egg else line)
            continue
        if line.startswith("-"):
            continue  # -r / -c / --index-url / --hash: options, not dependencies
        name = _dep_name(line.split(" #")[0])
        if name:
            names.append(name)
    return names, blind


def _package_json(text: str) -> list[str]:
    data = json.loads(text)
    names: list[str] = []
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        names += list(data.get(key, {}))
    for key in ("bundledDependencies", "bundleDependencies"):  # a list of names (or true: all of the above)
        bundled = data.get(key, [])
        names += [n for n in bundled if isinstance(n, str)] if isinstance(bundled, list) else []
    return list(dict.fromkeys(names))


def _js_module(spec: str) -> str | None:
    if spec.startswith((".", "/")):
        return None
    parts = spec.split("/")
    return "/".join(parts[:2]) if spec.startswith("@") else parts[0]


# Manifests of ecosystems the radar does not analyse: silently ignoring them would be a blind spot.
UNANALYSED_MANIFESTS = {
    "pipfile",
    "setup.py",
    "setup.cfg",
    "environment.yml",
    "environment.yaml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "cargo.toml",
    "gemfile",
    "composer.json",
}


def _is_requirements(rel: str) -> bool:
    """requirements*.txt, constraints*.txt, or any .txt under a requirements/ folder (audit A20, A21)."""
    parts = rel.lower().split("/")
    name = parts[-1]
    return name.endswith(".txt") and (
        name.startswith(("requirements", "constraints")) or "requirements" in parts[:-1]
    )


def _is_analysed_manifest(name: str, rel: str = "") -> bool:
    return name in ("pyproject.toml", "package.json") or _is_requirements(rel or name)


def _dockerfile_images(text: str, rel: str) -> list[Finding]:
    """Base images of a Dockerfile. A stage alias (`FROM builder`, `COPY --from=builder`) is not an image;
    `${ARG}` resolves to the ARG's default; what cannot be resolved is a blind spot, reported (A22-A24,
    A100: `COPY --from=<image>` pulls an image too)."""
    args = {name: default for name, default in _ARG.findall(text) if default}
    stages: set[str] = set()
    found: list[Finding] = []

    def image(ref: str) -> None:
        if ref.isdigit() or ref.lower() in stages or ref.lower() == "scratch":
            return
        resolved = _ARG_REF.sub(lambda m: args.get(m.group(1), m.group(0)), ref)
        if "$" in resolved:
            found.append(Finding(f"image {ref} (an unresolved build argument)", rel, "unanalysed"))
        else:
            found.append(Finding(_image_name(resolved), rel, "image"))

    for line in text.splitlines():
        from_line = _FROM.match(line)
        if from_line:
            image(from_line.group(1))
            if from_line.group(2):
                stages.add(from_line.group(2).lower())
            continue
        copy = _COPY_FROM.match(line)
        if copy:
            image(copy.group(1))
    return found


def _python_imports(text: str) -> set[str]:
    """Top-level modules a Python file imports (`import a, b` counts both: audit A98, A99). A file that does
    not parse falls back to the line regex."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return {(a or b).split(".")[0] for a, b in _PY_IMPORT.findall(text)}
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            mods.add(node.module.split(".")[0])
    return mods


def scan_file(path: Path, rel: str) -> list[Finding]:
    name = path.name.lower()
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        # An unreadable manifest must not read as "no dependencies": that would pass the radar gate.
        return [Finding(rel, rel, "error")] if _is_analysed_manifest(name, rel) else []
    if name in UNANALYSED_MANIFESTS:
        return [Finding(path.name, rel, "unanalysed")]
    try:
        if name == "pyproject.toml":
            return [Finding(n, rel, "manifest") for n in _pyproject(text)]
        if _is_requirements(rel):
            names, blind = _requirements(text)
            return [Finding(n, rel, "manifest") for n in names] + [
                Finding(f"editable install {b}", rel, "unanalysed") for b in blind
            ]
        if name == "package.json":
            return [Finding(n, rel, "manifest") for n in _package_json(text)]
    except (tomllib.TOMLDecodeError, json.JSONDecodeError, TypeError, AttributeError):
        return [Finding(rel, rel, "error")]  # the radar cannot see this manifest: reported, never ignored
    if name.startswith("dockerfile") or name.endswith(".dockerfile"):
        return _dockerfile_images(text, rel)
    if name.startswith(("docker-compose", "compose")) and name.endswith((".yml", ".yaml")):
        return [Finding(_image_name(m), rel, "image") for m in _COMPOSE_IMAGE.findall(text)]
    if name.endswith(".py"):
        return [Finding(m, rel, "import") for m in sorted(_python_imports(text))]
    if name.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")):
        mods = {m for m in map(_js_module, _JS_IMPORT.findall(text)) if m}
        return [Finding(m, rel, "import") for m in sorted(mods)]
    return []


def scan_project(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in iter_files(root):
        findings += scan_file(path, path.relative_to(root).as_posix())
    return findings
