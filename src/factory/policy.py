"""Dependency policies read from the lockfile and the installed packages (ROADMAP P3-3, P3-4).

Version constraints: a radar entry may pin what IT accepts (`version = ">=0.115"`); the gate compares it with
the version actually LOCKED in uv.lock, not with what the manifest asks for. Licences: the radar may forbid
licences (`[licenses] forbidden = ["AGPL", ...]`); the gate reads each installed package's licence from its
metadata in the app's .venv. No third-party dependency: a small comparator handles the usual specifiers.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from factory.radar import normalize

_CLAUSE = re.compile(r"^\s*(~=|==|!=|>=|<=|>|<)\s*([0-9][0-9A-Za-z.*+!-]*)\s*$")


class PolicyError(ValueError):
    pass


def _release(version: str) -> tuple[int, ...]:
    """The numeric release part: '0.115.4' -> (0, 115, 4); '2.0rc1' -> (2, 0); epochs and locals ignored."""
    version = version.split("!", 1)[-1].split("+", 1)[0]
    parts = []
    for piece in version.split("."):
        digits = re.match(r"\d+", piece)
        if not digits:
            break
        parts.append(int(digits.group()))
        if digits.group() != piece:  # '0rc1': the rest is a pre-release tag
            break
    return tuple(parts)


def _cmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    size = max(len(a), len(b))
    a, b = a + (0,) * (size - len(a)), b + (0,) * (size - len(b))
    return (a > b) - (a < b)


def parse_spec(spec: str) -> list[tuple[str, str]]:
    """'>=0.115, <1' -> [('>=', '0.115'), ('<', '1')]. Raises PolicyError on anything else."""
    clauses = []
    for raw in spec.split(","):
        m = _CLAUSE.match(raw)
        if not m:
            raise PolicyError(f"not a version constraint: {raw.strip()!r} in {spec!r}")
        clauses.append((m.group(1), m.group(2)))
    return clauses


def satisfies(version: str, spec: str) -> bool:
    have = _release(version)
    for op, target in parse_spec(spec):
        if target.endswith(".*"):  # ==1.2.* / !=1.2.*
            prefix = _release(target[:-2])
            match = have[: len(prefix)] == prefix
            if (op == "==" and not match) or (op == "!=" and match):
                return False
            continue
        want, c = _release(target), _cmp(have, _release(target))
        if op == "~=":  # compatible release: ~=1.4 -> >=1.4,<2 ; ~=1.4.2 -> >=1.4.2,<1.5
            upper = want[:-2] + (want[-2] + 1,) if len(want) >= 2 else (want[0] + 1,)
            ok = c >= 0 and _cmp(have, upper) < 0
        else:
            ok = {"==": c == 0, "!=": c != 0, ">=": c >= 0, "<=": c <= 0, ">": c > 0, "<": c < 0}[op]
        if not ok:
            return False
    return True


def locked_versions(app: Path) -> dict[str, str] | None:
    """{normalized package name: version} from the app's uv.lock; None when there is no lockfile."""
    lock = app / "uv.lock"
    if not lock.is_file():
        return None
    data = tomllib.loads(lock.read_text(encoding="utf-8"))
    return {
        normalize(p["name"]): str(p["version"])
        for p in data.get("package", [])
        if isinstance(p, dict) and p.get("name") and p.get("version")
    }


def _site_packages(app: Path) -> list[Path]:
    venv = app / ".venv"
    return [*venv.glob("Lib/site-packages"), *venv.glob("lib/python*/site-packages")]


def installed_licenses(app: Path) -> dict[str, str] | None:
    """{normalized package name: licence text} from the .dist-info METADATA of the app's environment;
    None when there is no installed environment. The text joins License-Expression, License and the licence
    classifiers, so 'GNU Affero General Public License v3' and 'AGPL-3.0' are both findable."""
    roots = _site_packages(app)
    if not roots:
        return None
    found: dict[str, str] = {}
    for root in roots:
        for meta in root.glob("*.dist-info/METADATA"):
            name, licence = "", []
            for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    break  # the headers end at the first blank line; the description follows
                key, _, value = line.partition(":")
                value = value.strip()
                if key == "Name":
                    name = value
                elif key in ("License-Expression", "License") and value and value.upper() != "UNKNOWN":
                    licence.append(value.splitlines()[0][:200])
                elif key == "Classifier" and value.startswith("License ::"):
                    licence.append(value.removeprefix("License ::").strip())
            if name:
                found[normalize(name)] = "; ".join(dict.fromkeys(licence))
    return found


def check_dependencies(app: Path, radar) -> list[str]:
    """Every dependency policy violation of the app, as readable lines ([] = compliant)."""
    problems: list[str] = []
    locked = locked_versions(app)
    if locked is None:
        return problems  # nothing locked, nothing to compare (the radar gate still judges the manifests)
    for tech in radar.techs:
        if not tech.version:
            continue
        # The constraint is about the package the entry is named after: `fastapi` pins fastapi, not the
        # other names that merely detect it (uvicorn, starlette), whose versions follow their own scheme.
        version = locked.get(normalize(tech.id))
        if version and not satisfies(version, tech.version):
            problems.append(f"{tech.id} {version} is locked; the radar allows {tech.name} {tech.version}")
    if radar.forbidden_licenses:
        licences = installed_licenses(app)
        if licences is None:
            problems.append("no installed environment (.venv) to read the licences from: run `uv sync` first")
            return problems
        for pkg in sorted(set(locked) & set(licences)):
            text = licences[pkg]
            hit = next((f for f in radar.forbidden_licenses if f.lower() in text.lower()), None)
            if hit:
                problems.append(f"{pkg} {locked[pkg]}: licence {text!r} matches the forbidden {hit!r}")
    return problems
