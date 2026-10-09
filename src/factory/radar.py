"""The company tech radar, as code.

IT owns radar.toml (which technology sits in which ring). This module owns the POLICY:
what each ring allows at each maturity rung. Keeping the matrix in code (reviewed, tested)
and the data in TOML (edited by IT) is the same split Claudo uses for its model registry.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

RINGS = ("adopt", "trial", "assess", "hold")
MATURITIES = ("pov", "poc", "mvp", "prod")

ALLOW = "allow"
APPROVAL = "needs_it_approval"
BLOCK = "block"

# ring -> maturity -> verdict. The higher the rung, the stricter.
POLICY: dict[str, dict[str, str]] = {
    "adopt": {"pov": ALLOW, "poc": ALLOW, "mvp": ALLOW, "prod": ALLOW},
    "trial": {"pov": ALLOW, "poc": ALLOW, "mvp": APPROVAL, "prod": APPROVAL},
    "assess": {"pov": ALLOW, "poc": APPROVAL, "mvp": BLOCK, "prod": BLOCK},
    "hold": {"pov": BLOCK, "poc": BLOCK, "mvp": BLOCK, "prod": BLOCK},
}
# A technology that is not on the radar at all.
UNKNOWN_POLICY = {"pov": APPROVAL, "poc": APPROVAL, "mvp": APPROVAL, "prod": BLOCK}


class RadarError(Exception):
    pass


def normalize(name: str) -> str:
    """Package-name normalization (PEP 503-ish): case, '_' and '.' are insignificant."""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


@dataclass(frozen=True)
class Tech:
    id: str
    name: str
    category: str
    ring: str
    match: tuple[str, ...]
    golden_path: str | None = None
    replaced_by: str | None = None
    preferred: bool = False
    note: str = ""
    # The name is also an ordinary word ("requests"): in free text it counts only in a code-like context.
    text_strict: bool = False
    version: str = ""  # versions IT accepts, PEP 440 style (">=0.115, <1"); checked against uv.lock
    # Only THESE aliases need a code-like context (`text_strict = ["motor"]`): "MongoDB" in prose still
    # counts, "motor insurance" does not (audit A83).
    strict_aliases: tuple[str, ...] = ()


def _alias_regex(alias: str) -> str:
    """An alias as a regex: `langchain-*` names a family (langchain-community, langchain-openai...)."""
    return re.escape(alias[:-1]) + r"[\w.-]+" if alias.endswith("*") else re.escape(alias)


def _strict_pattern(aliases: tuple[str, ...]) -> re.Pattern[str]:
    """Code-like mentions of an ambiguous name: `requests`, import requests, requests.get(, pip install
    requests, requests>=2, "the Requests library". Never the bare English word ("100 requests")."""
    a = "(?:" + "|".join(_alias_regex(x) for x in aliases) + ")"
    return re.compile(
        rf"`{a}`|\b(?:import|from)\s+{a}\b|\b{a}\.[A-Za-z_]\w*\s*\(|\b(?:pip|uv)\s+(?:install|add)\s+(?:\S+\s+)*?{a}\b"
        rf"|\b{a}\s*[=<>~!]=|\bthe\s+{a}\s+(?:library|package|module|client)\b",
        re.I,
    )


@dataclass
class Radar:
    company: str
    version: str
    techs: list[Tech]
    forbidden_licenses: tuple[str, ...] = ()  # [licenses] forbidden: substrings of licence texts

    def __post_init__(self) -> None:
        self._by_id = {t.id: t for t in self.techs}
        self._by_name: dict[str, Tech] = {}
        self._families: list[tuple[str, Tech]] = []  # (normalized prefix, tech) of `name-*` aliases
        for t in self.techs:
            for alias in (t.id, *t.match):
                if alias.endswith("*"):
                    self._families.append((normalize(alias[:-1]), t))
                else:
                    self._by_name.setdefault(normalize(alias), t)
        self._text_patterns: list[tuple[Tech, list[re.Pattern[str]]]] = []
        for t in self.techs:
            strict = t.match if t.text_strict else tuple(a for a in t.match if a in t.strict_aliases)
            loose = tuple(a for a in t.match if a not in strict)
            patterns = [_strict_pattern(strict)] if strict else []
            if loose:
                alternatives = "|".join(_alias_regex(a) for a in loose)
                patterns.append(re.compile(rf"(?<![\w-])(?:{alternatives})(?![\w-])", re.I))
            if patterns:
                self._text_patterns.append((t, patterns))

    def get(self, tech_id: str) -> Tech | None:
        return self._by_id.get(tech_id)

    def find(self, name: str) -> Tech | None:
        """Exact (normalized) lookup of a package / module / image / alias name, then the `name-*` families
        (langchain-community is LangChain: audit A82)."""
        key = normalize(name)
        exact = self._by_name.get(key)
        if exact is not None:
            return exact
        return next((t for prefix, t in self._families if key.startswith(prefix) and key != prefix), None)

    def scan_text(self, text: str) -> list[Tech]:
        """Technologies mentioned in free text (design docs, business ideas)."""
        return [t for t, patterns in self._text_patterns if any(p.search(text) for p in patterns)]

    def by_category(self, category: str) -> list[Tech]:
        return [t for t in self.techs if t.category == category]


def verdict(tech: Tech | None, maturity: str) -> str:
    if maturity not in MATURITIES:
        raise ValueError(f"unknown maturity {maturity!r} (expected one of {MATURITIES})")
    if tech is None:
        return UNKNOWN_POLICY[maturity]
    return POLICY[tech.ring][maturity]


def load_radar(path: Path) -> Radar:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise RadarError(f"tech radar not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise RadarError(f"{path}: {e}") from e

    errors: list[str] = []
    techs: list[Tech] = []
    seen: set[str] = set()
    owner: dict[str, str] = {}  # normalized alias -> tech id: one alias, one technology (audit A157)
    for i, raw in enumerate(data.get("tech", []), start=1):
        where = f"tech #{i} ({raw.get('id', '?')})"
        missing = [k for k in ("id", "name", "category", "ring") if not raw.get(k)]
        if missing:
            errors.append(f"{where}: missing {', '.join(missing)}")
            continue
        if raw["ring"] not in RINGS:
            errors.append(f"{where}: ring {raw['ring']!r} not in {RINGS}")
        if raw["id"] in seen:
            errors.append(f"{where}: duplicate id")
        seen.add(raw["id"])
        match = tuple(str(m).lower() for m in raw.get("match", []))
        for alias in {normalize(a) for a in (raw["id"], *match)}:
            if owner.setdefault(alias, raw["id"]) != raw["id"]:
                errors.append(f"{where}: alias {alias!r} already names {owner[alias]}")
        strict = raw.get("text_strict", False)
        if isinstance(strict, list):
            unknown = [a for a in strict if str(a).lower() not in match]
            if unknown:
                errors.append(f"{where}: text_strict names {unknown}, which are not in its match list")
        techs.append(
            Tech(
                id=raw["id"],
                name=raw["name"],
                category=raw["category"],
                ring=raw["ring"],
                match=match,
                golden_path=raw.get("golden_path"),
                replaced_by=raw.get("replaced_by"),
                preferred=bool(raw.get("preferred", False)),
                note=raw.get("note", ""),
                text_strict=strict is True,
                version=str(raw.get("version", "")).strip(),
                strict_aliases=tuple(str(a).lower() for a in strict) if isinstance(strict, list) else (),
            )
        )
    from factory.policy import PolicyError, parse_spec

    for t in techs:
        if t.version:
            try:
                parse_spec(t.version)
            except PolicyError as e:
                errors.append(f"tech {t.id}: {e}")
    forbidden = data.get("licenses", {}).get("forbidden", [])
    if not isinstance(forbidden, list) or not all(isinstance(x, str) and x.strip() for x in forbidden):
        errors.append("[licenses] forbidden must be a list of non-empty strings")
        forbidden = []
    for t in techs:
        if t.replaced_by and t.replaced_by not in seen:
            errors.append(f"tech {t.id}: replaced_by {t.replaced_by!r} is not on the radar")
    if errors:
        raise RadarError(f"invalid tech radar {path}:\n  - " + "\n  - ".join(errors))
    return Radar(
        company=data.get("company", ""),
        version=str(data.get("version", "")),
        techs=techs,
        forbidden_licenses=tuple(x.strip() for x in forbidden),
    )
