"""Find which technologies a project actually uses.

Sources, from strongest to weakest evidence:
  manifest  direct dependencies (pyproject.toml, requirements*.txt, package.json)
  image     Docker base images (Dockerfile FROM, compose `image:`)
  import    import statements in Python / JS / TS sources
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

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
    kind: str  # manifest | image | import


def iter_files(root: Path) -> Iterator[Path]:
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
    return [n for n in map(_dep_name, specs) if n]


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


def scan_file(path: Path, rel: str) -> list[Finding]:
    name = path.name.lower()
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    try:
        if name == "pyproject.toml":
            return [Finding(n, rel, "manifest") for n in _pyproject(text)]
        if name.startswith("requirements") and name.endswith(".txt"):
            return [Finding(n, rel, "manifest") for n in _requirements(text)]
        if name == "package.json":
            return [Finding(n, rel, "manifest") for n in _package_json(text)]
    except (tomllib.TOMLDecodeError, json.JSONDecodeError):
        return []
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
