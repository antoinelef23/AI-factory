"""Import a company's existing tech radar (CSV or JSON, Thoughtworks BYOR style) into radar.toml.

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


def _rows(text: str, source: str) -> tuple[list[dict[str, str]], list[str]]:
    text = text.lstrip("﻿")  # Excel's UTF-8 BOM
    if source.lower().endswith(".json"):
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("blips") or data.get("technologies") or data.get("tech") or []
        return [{str(k).strip().lower(): str(v).strip() for k, v in row.items()} for row in data], []
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
        tech_id = slugify(raw["name"])
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
                "replaced_by": slugify(raw["replaced_by"]) if raw["replaced_by"] else "",
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
