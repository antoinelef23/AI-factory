"""Quality gates run on a built app. Which gates apply depends on the maturity rung."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from factory.detect import iter_files
from factory.guard import check_project
from factory.radar import Radar

SECRET_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("generic API key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    (
        "hardcoded credential",
        re.compile(r"(?i)\b(?:password|passwd|secret|api_key|apikey|token)\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"),
    ),
]
TEXT_SUFFIXES = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".json",
    ".toml",
    ".yml",
    ".yaml",
    ".env",
    ".cfg",
    ".ini",
    ".md",
}

# (command, cwd) -> (returncode, combined output). Injectable for tests.
Executor = Callable[[str, Path], tuple[int, str]]


def shell_executor(command: str, cwd: Path) -> tuple[int, str]:
    try:
        p = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        return 124, f"timeout: {command}"
    return p.returncode, (p.stdout + p.stderr)[-4000:]


@dataclass(frozen=True)
class GateResult:
    name: str
    ok: bool
    detail: str


def secrets_gate(app_dir: Path) -> GateResult:
    hits: list[str] = []
    for path in iter_files(app_dir):
        if path.suffix.lower() not in TEXT_SUFFIXES and not path.name.startswith(".env"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pat in SECRET_PATTERNS:
            for m in pat.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                hits.append(f"{path.relative_to(app_dir).as_posix()}:{line}: {label}")
    return GateResult("secrets", not hits, "\n".join(hits) or "no secrets found")


def radar_gate(
    app_dir: Path, radar: Radar, maturity: str, exceptions: set[str], docs: list[Path]
) -> GateResult:
    report = check_project(app_dir, radar, maturity, docs=docs)
    blocking = report.blocking(exceptions)
    lines = [v.describe() for v in blocking]
    granted = [v.key for v in report.needs_approval() if v.key in exceptions]
    if granted:
        lines.append(f"IT-approved exceptions in use: {', '.join(granted)}")
    detail = "\n".join(lines) or f"all technologies allowed at {maturity}: {', '.join(report.allowed) or '-'}"
    return GateResult("radar", not blocking, detail)


def command_gate(name: str, command: str | None, app_dir: Path, executor: Executor) -> GateResult:
    if not command:
        return GateResult(
            name, False, f"no command configured for gate '{name}' in factory.toml [gates.commands]"
        )
    rc, out = executor(command, app_dir)
    return GateResult(name, rc == 0, f"$ {command}\n(exit {rc})\n{out.strip()}")


def run_gates(
    gates: list[str],
    app_dir: Path,
    *,
    radar: Radar,
    maturity: str,
    exceptions: set[str],
    docs: list[Path],
    commands: dict[str, str],
    executor: Executor = shell_executor,
    extra: dict[str, Callable[[], GateResult]] | None = None,
) -> list[GateResult]:
    """`extra` holds gates only the foreman can evaluate (they need the work item or the engine)."""
    results: list[GateResult] = []
    for gate in gates:
        if extra and gate in extra:
            results.append(extra[gate]())
        elif gate == "radar":
            results.append(radar_gate(app_dir, radar, maturity, exceptions, docs))
        elif gate == "secrets":
            results.append(secrets_gate(app_dir))
        else:
            results.append(command_gate(gate, commands.get(gate), app_dir, executor))
    return results


def format_report(results: list[GateResult], maturity: str) -> str:
    out = [f"# Gate report ({maturity})", ""]
    for r in results:
        out += [f"## {r.name}: {'PASS' if r.ok else 'FAIL'}", "", "```", r.detail, "```", ""]
    return "\n".join(out)


def secrets_in_history(app_dir: Path, rev_range: str) -> list[str]:
    """Secrets in the LINES ADDED by the commits of `rev_range`, `<sha>:<path>: <label>` each.

    The secrets gate reads the working tree, but a publish pushes every commit: a key committed once and
    deleted in a later commit is gone from the tree and still in the history that leaves the machine."""
    p = subprocess.run(
        ["git", "log", "-p", "--no-color", "--no-ext-diff", "--format=@@%H", rev_range],
        cwd=app_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if p.returncode != 0:
        raise ValueError(f"git log {rev_range} failed in {app_dir}: {(p.stderr or p.stdout).strip()[-300:]}")
    hits: list[str] = []
    sha = path = ""
    for line in p.stdout.splitlines():
        if line.startswith("@@") and len(line) >= 42:
            sha, path = line[2:10], ""
        elif line.startswith("+++ "):
            path = line[4:].removeprefix("b/")
        elif line.startswith("+") and sha:
            for label, pat in SECRET_PATTERNS:
                if pat.search(line):
                    hit = f"{sha}:{path}: {label}"
                    if hit not in hits:
                        hits.append(hit)
    return hits
