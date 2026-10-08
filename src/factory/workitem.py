"""Work items: one business idea travelling through the factory stages.

State lives in work/<slug>/item.json next to the artifacts (idea.md, spec.md, design.md,
tasks.md, gate-report.md), so the repo is the memory, as in Claudo.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path


@dataclass(frozen=True)
class Step:
    name: str
    kind: str  # auto | checkpoint | terminal
    role: str | None = None  # who decides at a checkpoint
    on_reject: str | None = None  # where a rejection sends the item back to
    summary: str = ""


STEPS: tuple[Step, ...] = (
    Step("triage", "auto", summary="classify the idea, spot technologies the business mentioned"),
    Step("spec", "auto", summary="write spec.md (the WHAT)"),
    Step("spec_review", "checkpoint", "business", "spec", "business validates the spec"),
    Step("design", "auto", summary="compile design.md from the tech radar (the HOW)"),
    Step("design_review", "checkpoint", "it", "design", "IT validates stack and radar exceptions"),
    Step("plan", "auto", summary="write tasks.md (the DO)"),
    Step("plan_review", "checkpoint", "owner", "plan", "owner approves the execution plan"),
    Step("build", "auto", summary="scaffold from the golden path, then implement"),
    Step("gate", "auto", summary="radar, secrets, tests, lint per maturity"),
    Step("ship_review", "checkpoint", "it", "build", "IT reviews and merges (always human)"),
    Step("shipped", "terminal", summary="delivered"),
)
STEP_BY_NAME = {s.name: s for s in STEPS}
ROLES = ("business", "it", "owner")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:48].rstrip("-") or "item"


@dataclass
class WorkItem:
    slug: str
    title: str
    idea: str
    maturity: str
    requester: str = "business"
    kind: str = "app"  # app | feature | bug | migration (the last three change an EXISTING app)
    target: str = ""  # for a change: the slug of the shipped app it works on
    base_branch: str = ""  # a change runs on factory/<slug>; the branch it started from
    base_sha: str = ""
    merged: bool = (
        False  # IT merged the change branch (`factory merge`, or the pull request + `factory sync`)
    )
    repo: str = ""  # owner/name of the app's private repository once published
    repo_url: str = ""  # the remote the app pushes to
    pr_url: str = ""  # the pull request opened for a change
    pr_state: str = ""  # OPEN | MERGED | CLOSED, as last seen by `factory sync`
    created: str = field(default_factory=now)
    stage: str = "triage"
    status: str = "active"  # active | waiting | blocked | shipped
    capabilities: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    feedback: str = ""  # last rejection reason / failed gate report, fed to the next attempt
    it_exceptions: list[str] = field(default_factory=list)  # radar keys IT explicitly approved
    # key -> {"expires": "YYYY-MM-DD" or "", "reason": str, "by": str}: why, who, and until when
    exception_terms: dict = field(default_factory=dict)
    skip_design_review: bool = False
    approvals: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    cost_usd: float = 0.0
    judgements: dict = field(default_factory=dict)  # kind -> {verdict, average, summary}; advisory only
    build_attempts: int = 0  # total build runs, for telemetry (the retry budget is per `run`)
    approved_hashes: dict = field(default_factory=dict)  # spec.md / design.md sha256 at human approval
    claudo_review: dict = field(default_factory=dict)  # {cp, verdict, report}: Claudo's reviewer, shown to IT
    claudo_cost_seen: float = 0.0  # Claudo journal spend already added to cost_usd
    claudo_cp: str = ""  # human checkpoint Claudo is paused at (e.g. CP-1); "" = none pending
    approval_nonce: str = ""  # per-round nonce the signed approval is bound to (a stale token cannot replay)
    claudo_rejection: dict = field(default_factory=dict)  # {cp, reason, by}: IT rejected, rework pending

    @property
    def step(self) -> Step:
        return STEP_BY_NAME[self.stage]

    @property
    def change_open(self) -> bool:
        """A change to an existing app, neither merged nor abandoned: it owns that app's branch space."""
        return self.kind != "app" and not self.merged and self.status != "abandoned"

    def expiry_of(self, key: str) -> str:
        return str(self.exception_terms.get(key, {}).get("expires", ""))

    def active_exceptions(self, today: date) -> set[str]:
        """Exceptions still in force: granted and not past their expiry day (the expiry day itself counts)."""
        return {
            k for k in self.it_exceptions if not self.expiry_of(k) or today.isoformat() <= self.expiry_of(k)
        }

    def expired_exceptions(self, today: date) -> list[str]:
        return sorted(set(self.it_exceptions) - self.active_exceptions(today))

    def log(self, event: str, detail: str = "") -> None:
        self.history.append({"at": now(), "stage": self.stage, "event": event, "detail": detail})

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> WorkItem:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


class Store:
    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir

    def dir(self, slug: str) -> Path:
        return self.work_dir / slug

    def exists(self, slug: str) -> bool:
        return (self.dir(slug) / "item.json").is_file()

    def new_slug(self, title: str) -> str:
        base = slugify(title)
        slug, n = base, 2
        while self.exists(slug):
            slug, n = f"{base}-{n}", n + 1
        return slug

    def save(self, item: WorkItem) -> None:
        d = self.dir(item.slug)
        d.mkdir(parents=True, exist_ok=True)
        (d / "item.json").write_text(
            json.dumps(item.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
        )

    def load(self, slug: str) -> WorkItem:
        path = self.dir(slug) / "item.json"
        if not path.is_file():
            raise KeyError(f"no work item '{slug}' in {self.work_dir}")
        return WorkItem.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def all(self) -> list[WorkItem]:
        if not self.work_dir.is_dir():
            return []
        items = [self.load(p.parent.name) for p in sorted(self.work_dir.glob("*/item.json"))]
        return sorted(items, key=lambda i: i.created)

    def write(self, item: WorkItem, name: str, content: str) -> Path:
        path = self.dir(item.slug) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")  # LF on every OS: clean git diffs
        return path

    def read(self, item: WorkItem, name: str) -> str:
        path = self.dir(item.slug) / name
        return path.read_text(encoding="utf-8") if path.is_file() else ""
