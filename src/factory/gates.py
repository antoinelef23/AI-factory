"""Quality gates run on a built app. Which gates apply depends on the maturity rung."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from factory.detect import iter_files
from factory.guard import check_project
from factory.policy import check_dependencies
from factory.project import ProjectError, run_git
from factory.radar import Radar

# A credential-looking name: DB_PASSWORD, client_secret, SECRET_KEY, "password", GITHUB_TOKEN, api-key...
_CREDENTIAL_NAME = r"[A-Za-z0-9_.-]*(?:password|passwd|secret|api[_-]?key|apikey|token)(?:_?key)?"
# A value that is a reference, not a secret: ${VAR}, {{ template }}, <placeholder>, %(name)s.
_REFERENCE = r"(?![$<{%])"

SECRET_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    # OpenAI project / service-account / admin keys carry a second dash (audit A54).
    ("generic API key", re.compile(r"\bsk-(?!ant-)(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{32,}")),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Stripe live key", re.compile(r"\b[rs]k_live_[0-9a-zA-Z]{24,}\b")),
    (  # quoted, in any file: DB_PASSWORD = "...", "password": "...", SECRET_KEY='...' (audit A55)
        "hardcoded credential",
        re.compile(rf"(?i)\b{_CREDENTIAL_NAME}['\"]?\s*[:=]\s*['\"]{_REFERENCE}[^'\"\s]{{8,}}['\"]"),
    ),
]
# Unquoted `NAME=value` lines: only in env-style files, where that is how a value is written.
ENV_SECRET_PATTERNS = [
    (
        "hardcoded credential",
        re.compile(
            rf"(?im)^\s*(?:export\s+)?{_CREDENTIAL_NAME}\s*[=:]\s*{_REFERENCE}(?!['\"])[^\s#'\"]{{8,}}"
        ),
    ),
]
ENV_STYLE_SUFFIXES = {".env", ".properties", ".ini", ".cfg", ".conf"}


def secret_patterns_for(name: str) -> list[tuple[str, re.Pattern]]:
    """The patterns that apply to a file named `name` (its base name or path)."""
    base = name.rsplit("/", 1)[-1].lower()
    env_style = base.startswith(".env") or any(base.endswith(s) for s in ENV_STYLE_SUFFIXES)
    return SECRET_PATTERNS + (ENV_SECRET_PATTERNS if env_style else [])


def read_text_file(path: Path) -> str | None:
    """A file's text whatever its extension or encoding; None for a binary (NUL byte) or unreadable file.
    Scanning by sniffing, not by an extension allowlist, and never skipping a non-UTF-8 file (A56, A57)."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


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
    # Everything that will be published, vendored code included (audit A101).
    for path in iter_files(app_dir, include_dependencies=True):
        text = read_text_file(path)
        if text is None:
            continue
        for label, pat in secret_patterns_for(path.name):
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


def dependencies_gate(app_dir: Path, radar: Radar) -> GateResult:
    """Versions locked in uv.lock and package-lock.json against the radar's constraints; installed licences
    against its policy. A manifest without its lockfile fails: nothing pins what ships (A138)."""
    problems = check_dependencies(app_dir, radar)
    detail = "\n".join(problems) or "locked versions and licences comply with the radar"
    return GateResult("dependencies", not problems, detail)


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
        elif gate == "dependencies":
            results.append(dependencies_gate(app_dir, radar))
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
    deleted in a later commit is gone from the tree and still in the history that leaves the machine. Merge
    commits are diffed too (`-m`): a key can be introduced in a merge resolution. A key seen in several
    commits (a branch commit and the merge that brings it in) is reported once, at the newest commit."""
    # --text --no-textconv: a .gitattributes the agent wrote (`*.env -diff`) cannot hide a file's lines (A58).
    try:
        p = run_git(
            app_dir,
            "log", "-p", "-m", "--text", "--no-textconv", "--no-color", "--no-ext-diff",
            "--format=%x00commit %H", rev_range,
        )  # fmt: skip
    except ProjectError as e:
        raise ValueError(str(e)) from e
    if p.returncode != 0:
        raise ValueError(f"git log {rev_range} failed in {app_dir}: {(p.stderr or p.stdout).strip()[-300:]}")
    hits: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    sha = path = ""
    in_header = False  # between `diff --git` and the first `@@`: file headers, never content
    for line in p.stdout.splitlines():
        if line.startswith("\x00commit "):
            sha, path, in_header = line[8:16], "", False
        elif line.startswith("diff --git "):
            path, in_header = line.split(" b/", 1)[1] if " b/" in line else "", True
        elif line.startswith("@@"):
            in_header = False
        elif not in_header and line.startswith("+") and sha:
            for label, pat in secret_patterns_for(path):
                key = (path, label, line[1:].strip())
                if pat.search(line[1:]) and key not in seen:
                    seen.add(key)
                    hits.append(f"{sha}:{path}: {label}")
    return hits
