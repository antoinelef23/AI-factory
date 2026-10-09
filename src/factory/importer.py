"""Import a company's existing tech radar into radar.toml: a spreadsheet (CSV, Thoughtworks BYOR style),
a BYOR JSON list, or the JSON of Backstage's tech-radar plugin (`quadrants`, `rings`, timed `entries`).

Companies keep their radar in a spreadsheet, not in TOML. What they usually DON'T have is a capability
category per technology ("backend", "database"), which the design compiler needs to pick a stack: the
importer says so instead of guessing, and keeps what it can derive from the quadrant.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field

from factory.radar import RINGS
from factory.workitem import slugify

RING_ALIASES = {
    "adopt": "adopt",
    "adopter": "adopt",
    "trial": "trial",
    "essayer": "trial",
    "assess": "assess",
    "evaluer": "assess",
    "évaluer": "assess",
    "hold": "hold",
    "suspendre": "hold",
    "eviter": "hold",
    "éviter": "hold",
}
# Thoughtworks quadrants are not capabilities: they are kept as a hint only.
QUADRANT_CATEGORY = {
    "techniques": "technique",
    "tools": "tool",
    "platforms": "platform",
    "languages & frameworks": "framework",
    "languages and frameworks": "framework",
    # Backstage's default quadrants
    "infrastructure": "platform",
    "frameworks": "framework",
    "languages": "language",
    "process": "technique",
}
COLUMN_ALIASES = {
    "name": ("name", "nom", "technology", "technologie", "blip"),
    "ring": ("ring", "anneau", "status", "statut"),
    "quadrant": ("quadrant",),
    "category": ("category", "categorie", "catégorie", "capability", "capacite", "capacité"),
    "match": ("match", "aliases", "alias"),
    "replaced_by": ("replaced_by", "replacement", "remplace_par", "remplacement"),
    "golden_path": ("golden_path", "goldenpath"),
}
DELIMITERS = ",;\t|"


@dataclass
class ImportResult:
    toml: str = ""
    counts: dict[str, int] = field(default_factory=dict)  # ring -> number of technologies
    problems: list[str] = field(default_factory=list)  # block the import
    warnings: list[str] = field(default_factory=list)  # IT should look at them
    needs_category: list[str] = field(
        default_factory=list
    )  # tech ids that cannot be selected for a stack yet

    @property
    def ok(self) -> bool:
        return not self.problems


def _sniff(text: str) -> str:
    """The delimiter: French Excel writes `;`, others `,` or tabs. The header line decides."""
    header = text.lstrip("﻿").splitlines()[0] if text.strip() else ""
    best = max(DELIMITERS, key=lambda d: header.count(d))
    return best if header.count(best) else ","


def _is_backstage(data: object) -> bool:
    return (
        isinstance(data, dict)
        and isinstance(data.get("entries"), list)
        and ("rings" in data or "quadrants" in data)
    )


def _backstage_rows(data: dict) -> list[dict[str, str]]:
    """Backstage tech-radar JSON (TechRadarLoaderResponse) as importer rows.

    An entry's ring is its MOST RECENT timeline move (by date; the first move when dates are missing).
    Ring and quadrant ids are the company's own: they are resolved through `rings` / `quadrants` to their
    names, and the ring name then goes through the usual aliases (ADOPT, Trial, ...)."""
    rings = {str(r.get("id", "")).strip(): str(r.get("name", "")).strip() for r in data.get("rings", [])}
    quadrants = {
        str(q.get("id", "")).strip(): str(q.get("name", "")).strip() for q in data.get("quadrants", [])
    }
    rows: list[dict[str, str]] = []
    for entry in data["entries"]:
        timeline = [t for t in entry.get("timeline") or [] if isinstance(t, dict)]
        dated = [t for t in timeline if t.get("date")]
        current = max(dated, key=lambda t: str(t["date"])) if dated else (timeline[0] if timeline else {})
        ring_id = _cell(current.get("ringId", entry.get("ring", "")))
        ring = ring_id if ring_id.lower() in RING_ALIASES else rings.get(ring_id, ring_id)
        quadrant_id = _cell(entry.get("quadrant", ""))
        name = _cell(entry.get("title") or entry.get("id") or "")
        key = _cell(entry.get("key") or entry.get("id") or "")
        rows.append(
            {
                "name": name,
                "ring": ring,
                "quadrant": quadrants.get(quadrant_id, quadrant_id),
                # optional fields a company may add to its entries; standard Backstage has none of them
                "category": _cell(entry.get("category", "")),
                "replaced_by": _cell(entry.get("replacedBy") or entry.get("replaced_by") or ""),
                "match": key if key and key.lower() != name.lower() else "",
            }
        )
    return rows


def _cell(value: object) -> str:
    """A JSON value as the importer's text: null is empty, a list is `a;b` (never 'None' or "['a']": A59)."""
    if value is None:
        return ""
    if isinstance(value, list):
        return ";".join(_cell(v) for v in value if v is not None)
    return str(value).strip()


def tech_slug(name: str) -> str:
    """A radar id for a technology name: C, C++ and C# must not all become 'c'."""
    return slugify(name.replace("++", "pp").replace("#", "sharp").replace("+", "plus"))


# An alias that is also an everyday word (or too short to be one) matches design prose by accident: "Go" on
# hold once blocked "let users go to...". Such aliases only count in code-like contexts (A60, A83).
COMMON_WORDS = {
    "go", "rust", "swift", "dart", "spring", "express", "rails", "requests", "motor", "click", "black",
    "next", "remix", "ember", "backbone", "spark", "hive", "storm", "puppet", "chef", "salt", "vault",
    "consul", "nomad",
}  # fmt: skip


def _needs_strict(aliases: list[str]) -> bool:
    return any(len(a) <= 3 or a in COMMON_WORDS for a in aliases)


def _rows(text: str, source: str) -> tuple[list[dict[str, str]], list[str]]:
    text = text.lstrip("﻿")  # Excel's UTF-8 BOM
    if source.lower().endswith(".json"):
        data = json.loads(text)
        if _is_backstage(data):
            return _backstage_rows(data), []
        if isinstance(data, dict):
            data = data.get("blips") or data.get("technologies") or data.get("tech") or []
        return [{str(k).strip().lower(): _cell(v) for k, v in row.items()} for row in data], []
    reader = csv.DictReader(io.StringIO(text), delimiter=_sniff(text))
    rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in row.items()} for row in reader]
    return rows, []


def _col(row: dict[str, str], key: str) -> str:
    return next((row[a] for a in COLUMN_ALIASES[key] if row.get(a)), "")


def _toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # a JSON string is a valid TOML basic string


def _toml_list(values: list[str]) -> str:
    return "[" + ", ".join(_toml_str(v) for v in values) + "]"


def _entry(row: dict[str, str]) -> dict[str, str]:
    return {
        "name": _col(row, "name"),
        "ring": _col(row, "ring"),
        "quadrant": _col(row, "quadrant"),
        "category": _col(row, "category").strip().lower(),
        "match": _col(row, "match"),
        "replaced_by": _col(row, "replaced_by"),
        "golden_path": _col(row, "golden_path"),
    }


def _render(entry: dict) -> str:
    lines = ["[[tech]]", f"id = {_toml_str(entry['id'])}", f"name = {_toml_str(entry['name'])}"]
    lines += [f"category = {_toml_str(entry['category'])}", f"ring = {_toml_str(entry['ring'])}"]
    lines.append(f"match = {_toml_list(entry['match'])}")
    if entry["replaced_by"]:
        lines.append(f"replaced_by = {_toml_str(entry['replaced_by'])}")
    if entry["golden_path"]:
        lines.append(f"golden_path = {_toml_str(entry['golden_path'])}")
    if _needs_strict(entry["match"]):
        lines.append("text_strict = true")
    return "\n".join(lines)


def import_radar(
    text: str, *, source: str = "radar.csv", company: str = "", version: str = ""
) -> ImportResult:
    result = ImportResult()
    try:
        rows, _ = _rows(text, source)
    except (json.JSONDecodeError, AttributeError, TypeError) as e:
        result.problems.append(f"cannot read {source}: {e}")
        return result
    if not rows:
        result.problems.append(f"{source}: no technology found (empty file or unrecognized header)")
        return result
    columns = set().union(*(r.keys() for r in rows))  # the header decides, not whether values are filled in
    if not columns & set(COLUMN_ALIASES["name"]) or not columns & set(COLUMN_ALIASES["ring"]):
        result.problems.append(
            f"{source}: needs a name column ({', '.join(COLUMN_ALIASES['name'])}) and a ring column "
            f"({', '.join(COLUMN_ALIASES['ring'])})"
        )
        return result

    entries: list[dict] = []
    seen: dict[str, str] = {}
    result.counts = dict.fromkeys(RINGS, 0)
    for line_no, row in enumerate(rows, start=2):  # line 1 is the header
        raw = _entry(row)
        if not raw["name"] and not raw["ring"]:
            continue  # blank row
        if not raw["name"]:
            result.problems.append(f"line {line_no}: a technology without a name")
            continue
        ring = RING_ALIASES.get(raw["ring"].strip().lower())
        if ring is None:
            result.problems.append(
                f"line {line_no}: {raw['name']!r} has ring {raw['ring']!r}, "
                f"expected one of {sorted(set(RING_ALIASES))}"
            )
            continue
        tech_id = tech_slug(raw["name"])
        if tech_id in seen:
            result.problems.append(
                f"line {line_no}: {raw['name']!r} duplicates {seen[tech_id]!r} (id {tech_id!r})"
            )
            continue
        seen[tech_id] = raw["name"]
        category = raw["category"] or QUADRANT_CATEGORY.get(raw["quadrant"].strip().lower(), "uncategorized")
        if not raw["category"]:
            result.needs_category.append(tech_id)
        aliases = [raw["name"].lower(), tech_id]
        aliases += [m.strip().lower() for m in raw["match"].replace("|", ";").split(";") if m.strip()]
        entries.append(
            {
                "id": tech_id,
                "name": raw["name"],
                "category": category,
                "ring": ring,
                "match": list(dict.fromkeys(aliases)),  # unique, order kept
                "replaced_by": tech_slug(raw["replaced_by"]) if raw["replaced_by"] else "",
                "golden_path": raw["golden_path"],
            }
        )
        result.counts[ring] += 1

    for e in entries:  # a replacement must be on the radar, or the loader would reject the file
        if e["replaced_by"] and e["replaced_by"] not in seen:
            result.problems.append(f"{e['id']}: replaced_by {e['replaced_by']!r} is not on the radar")
    if result.needs_category:
        result.warnings.append(
            f"{len(result.needs_category)} technologies have no capability category (backend, ...): "
            "they stay enforceable (hold/trial/assess) but cannot be chosen for a stack. Add a `category` "
            f"column. First: {', '.join(result.needs_category[:8])}"
        )
    if result.counts["hold"] and not any(e["replaced_by"] for e in entries):
        result.warnings.append(
            "no `replaced_by` given for any hold technology: no alternative can be suggested"
        )
    header = [f"company = {_toml_str(company)}", f"version = {_toml_str(version)}"]
    comment = [
        "# Imported radar: review it, then give each technology a capability `category` and, for hold",
        "# technologies, a `replaced_by`. Rings: adopt | trial | assess | hold.",
    ]
    sections = ["\n".join(comment), "\n".join(header), *(_render(e) for e in entries)]
    result.toml = "\n\n".join(sections) + "\n"
    return result
