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


@dataclass
class Radar:
    company: str
    version: str
    techs: list[Tech]

    def __post_init__(self) -> None:
        self._by_id = {t.id: t for t in self.techs}
        self._by_name: dict[str, Tech] = {}
        for t in self.techs:
            for alias in (t.id, *t.match):
                self._by_name.setdefault(normalize(alias), t)
        self._text_patterns = [
            (t, re.compile(r"(?<![\w-])(?:" + "|".join(re.escape(a) for a in t.match) + r")(?![\w-])", re.I))
            for t in self.techs
            if t.match
        ]

    def get(self, tech_id: str) -> Tech | None:
        return self._by_id.get(tech_id)

    def find(self, name: str) -> Tech | None:
        """Exact (normalized) lookup of a package / module / image / alias name."""
        return self._by_name.get(normalize(name))

    def scan_text(self, text: str) -> list[Tech]:
        """Technologies mentioned in free text (design docs, business ideas)."""
        return [t for t, pat in self._text_patterns if pat.search(text)]

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
        techs.append(
            Tech(
                id=raw["id"],
                name=raw["name"],
                category=raw["category"],
                ring=raw["ring"],
                match=tuple(str(m).lower() for m in raw.get("match", [])),
                golden_path=raw.get("golden_path"),
                replaced_by=raw.get("replaced_by"),
                preferred=bool(raw.get("preferred", False)),
                note=raw.get("note", ""),
            )
        )
    for t in techs:
        if t.replaced_by and t.replaced_by not in seen:
            errors.append(f"tech {t.id}: replaced_by {t.replaced_by!r} is not on the radar")
    if errors:
        raise RadarError(f"invalid tech radar {path}:\n  - " + "\n  - ".join(errors))
    return Radar(company=data.get("company", ""), version=str(data.get("version", "")), techs=techs)
