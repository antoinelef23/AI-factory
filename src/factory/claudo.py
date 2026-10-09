"""Bridge to the Claudo engine (the orchestrator + plan-lint of AI-Workflow-gates).

The factory does NOT re-implement plan validation: it runs Claudo's own `orchestrate.py --validate`
on a throwaway project holding the triplet, so a plan the factory approves is, by construction, a
plan Claudo will execute. Claudo is located, never vendored (ROADMAP D2): `[engine] claudo` in
factory.toml, then $CLAUDO_HOME, then a sibling checkout.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

ENGINE_ENTRY = Path("lab") / "engine" / "orchestrate.py"
SIBLING_GUESSES = ("AI-Workflow-gates/_build", "Claudo")  # relative to the factory root's parent


class EngineError(Exception):
    pass


@dataclass
class LintResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors

    def feedback(self) -> str:
        """What to tell an agent so it can fix its plan."""
        lines = [f"- {e}" for e in self.errors] + [f"- (warning) {w}" for w in self.warnings]
        return "\n".join(lines)


def is_claudo(home: Path) -> bool:
    return (home / ENGINE_ENTRY).is_file()


def discover(configured: str | None, factory_root: Path) -> Path | None:
    """Locate a Claudo checkout. Explicit config wins; an explicit but invalid path is an error."""
    candidates: list[Path] = []
    if configured:
        explicit = Path(configured)
        explicit = explicit if explicit.is_absolute() else factory_root / explicit
        if not is_claudo(explicit):
            raise EngineError(f"[engine] claudo = {configured!r}: no {ENGINE_ENTRY} under {explicit}")
        return explicit.resolve()
    if os.environ.get("CLAUDO_HOME"):
        candidates.append(Path(os.environ["CLAUDO_HOME"]))
    candidates += [factory_root.parent / guess for guess in SIBLING_GUESSES]
    return next((c.resolve() for c in candidates if is_claudo(c)), None)


def parse_lint_output(output: str, returncode: int) -> LintResult:
    """Claudo prints one `❌ ...` line per error and one `⚠️ ...` line per warning."""
    result = LintResult(raw=output)
    for line in output.splitlines():
        text = line.strip()
        if text.startswith("❌"):
            result.errors.append(text.removeprefix("❌").strip())
        elif text.startswith("⚠"):
            result.warnings.append(text.lstrip("⚠️ ").strip())
    if returncode != 0 and not result.errors:  # crashed or refused without a parsable message
        result.errors.append(f"plan-lint exited {returncode}: {output.strip()[-400:] or 'no output'}")
    return result


CHECKPOINT_WAIT = re.compile(r"⏸\s+(CP-\d+)\s+—\s+waiting for")  # what orchestrate.py prints when it pauses


@dataclass
class BuildResult:
    """Outcome of one orchestrator run: it either reached a human checkpoint, finished, or failed."""

    outcome: str  # checkpoint | done | failed | timeout
    checkpoint: str = ""  # e.g. CP-1 when outcome == "checkpoint"
    log: str = ""
    returncode: int | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in ("checkpoint", "done")


class ClaudoEngine:
    def __init__(self, home: Path, python: str | None = None) -> None:
        if not is_claudo(home):
            raise EngineError(f"not a Claudo checkout: {home}")
        self.home = home
        self.python = python or sys.executable

    def lint_plan(self, slug: str, *, spec: str, design: str, tasks: str, timeout: int = 60) -> LintResult:
        """Plan-lint the triplet with Claudo's real validator (acyclic DAG, spec IDs exist, disjoint
        parallel paths, allowlisted verify commands, checkpoints...)."""
        with tempfile.TemporaryDirectory(prefix="factory-lint-", ignore_cleanup_errors=True) as tmp:
            project = Path(tmp)
            feat = project / "work" / slug
            feat.mkdir(parents=True)
            for name, text in (("spec.md", spec), ("design.md", design), ("tasks.md", tasks)):
                (feat / name).write_text(text, encoding="utf-8", newline="\n")
            (project / "pyproject.toml").write_text(
                "[project]\nname = 'lint'\nversion = '0'\n", encoding="utf-8"
            )
            argv = [self.python, str(self.home / ENGINE_ENTRY), f"work/{slug}", "--project", str(project)]
            try:
                p = subprocess.run(
                    [*argv, "--validate"],
                    cwd=self.home,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    env={**os.environ, "LAB_NO_NOTIFY": "1", "PYTHONUTF8": "1"},
                )
            except subprocess.TimeoutExpired as e:
                raise EngineError(f"plan-lint timed out after {timeout}s") from e
        return parse_lint_output(p.stdout + p.stderr, p.returncode)

    def run_build(
        self,
        slug: str,
        project: Path,
        *,
        stop_at_checkpoint: bool = True,
        env: dict[str, str] | None = None,
        timeout: int = 3600,
    ) -> BuildResult:
        """Run Claudo's orchestrator on `work/<slug>` of `project` (the app repo).

        Claudo BLOCKS (polls) at a blocking checkpoint until a signed human approval appears. The factory's
        process model is one short CLI call per human decision, so by default the orchestrator is stopped
        the moment it announces the checkpoint: its state (`.runs/state.json`) is persisted, and a later
        call resumes where it paused (finished tasks are skipped)."""
        argv = [self.python, str(self.home / ENGINE_ENTRY), f"work/{slug}", "--project", str(project)]
        proc = subprocess.Popen(
            argv,
            cwd=self.home,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "LAB_NO_NOTIFY": "1", "PYTHONUTF8": "1", **(env or {})},
            **OWN_PROCESS_GROUP,
        )
        timed_out = threading.Event()

        def expire() -> None:
            timed_out.set()
            _stop(proc)

        watchdog = threading.Timer(timeout, expire)
        watchdog.start()
        lines: list[str] = []
        checkpoint = ""
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.append(line)
                found = CHECKPOINT_WAIT.search(line)
                if found and stop_at_checkpoint:
                    checkpoint = found.group(1)
                    _stop(proc)
                    break
            proc.wait()
        finally:
            watchdog.cancel()
            if proc.poll() is None:
                _stop(proc)
        log = "".join(lines)
        if timed_out.is_set():
            return BuildResult("timeout", log=log, returncode=proc.returncode)
        if checkpoint:
            return BuildResult("checkpoint", checkpoint, log, proc.returncode)
        return BuildResult("done" if proc.returncode == 0 else "failed", log=log, returncode=proc.returncode)

    def sign_approval(
        self, project: Path, slug: str, cp: str, author: str, secret: str, nonce: str = ""
    ) -> Path:
        """Write the HMAC-signed approval token the orchestrator waits for (Claudo's approvals.py, the same
        code path as its approve.sh). The secret is passed to this one process only."""
        feature = project / "work" / slug
        try:
            p = subprocess.run(
                [
                    self.python,
                    str(self.home / "lab" / "engine" / "approvals.py"),
                    "sign",
                    cp,
                    str(feature),
                    author,
                    *(["--nonce", nonce] if nonce else []),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env={**os.environ, "LAB_APPROVAL_SECRET": secret, "PYTHONUTF8": "1"},
                timeout=60,
            )
        except subprocess.TimeoutExpired as e:
            raise EngineError(f"signing {cp} did not finish within 60s") from e
        if p.returncode != 0:
            raise EngineError(f"signing {cp} failed: {(p.stderr or p.stdout).strip()[-300:]}")
        return feature / ".approvals" / cp

    def trajectory(self, project: Path, slug: str, timeout: int = 60) -> tuple[bool, str]:
        """Claudo's trajectory guard: not WHAT the build produced but HOW it got there (a `task_done`
        without a successful attempt = forged journal; a commit touching files outside the task's scope).
        Returns (ok, output). Read-only; no journal means nothing to check. A hung guard fails the gate."""
        try:
            p = self._trajectory_process(project, slug, timeout)
        except subprocess.TimeoutExpired:
            return False, f"the trajectory guard did not finish within {timeout}s"
        return p.returncode == 0, (p.stdout + p.stderr).strip()

    def _trajectory_process(self, project: Path, slug: str, timeout: int) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.python, str(self.home / "lab" / "engine" / "trajectory_guard.py"), f"work/{slug}"]
            + ["--root", str(project)],
            cwd=self.home,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, "PYTHONUTF8": "1"},
        )

    @staticmethod
    def review_verdict(project: Path, slug: str, cp: str) -> tuple[str, str] | None:
        """(verdict, report path relative to the project) of Claudo's reviewer panel at checkpoint `cp`,
        or None when no report exists. The verdict is what the human decides on: PASS | WARN | BLOCK."""
        report = project / "work" / slug / ".runs" / f"{cp}-review.md"
        if not report.is_file():
            return None
        head = report.read_text(encoding="utf-8", errors="replace")[:600]
        match = re.search(r"aggregated verdict:\s*(PASS|WARN|BLOCK)", head)
        return (match.group(1) if match else "UNKNOWN", report.relative_to(project).as_posix())

    @staticmethod
    def journal_cost(project: Path, slug: str) -> float:
        """Total model spend Claudo recorded for this feature (tolerant: a torn line or no journal is 0)."""
        path = project / "work" / slug / ".runs" / "journal.jsonl"
        if not path.is_file():
            return 0.0
        total = 0.0
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                total += float(json.loads(line).get("cost_usd") or 0.0)
            except (ValueError, TypeError, AttributeError):
                continue
        return total

    @staticmethod
    def reject_checkpoint(project: Path, slug: str, cp: str, reason: str, by: str) -> Path:
        """Write `.approvals/<cp>.rejected` as Claudo's reject.sh does: the orchestrator reopens the
        checkpoint's tasks and passes `reason` verbatim to the agents."""
        d = project / "work" / slug / ".approvals"
        d.mkdir(parents=True, exist_ok=True)
        clean = " ".join(reason.split())  # one line: the file format is `key=value` per line
        path = d / f"{cp}.rejected"
        path.write_text(f"reason={clean}\nby={by}\n", encoding="utf-8", newline="\n")
        return path

    def supports_nonce(self) -> bool:
        """True when this Claudo binds approval tokens to a per-round nonce (approvals.ENV_NONCE, Claudo
        b9b96c7 and later). An older one silently ignores the nonce: replay protection is then not there."""
        try:
            return "ENV_NONCE" in (self.home / "lab" / "engine" / "approvals.py").read_text(encoding="utf-8")
        except OSError:
            return False

    @staticmethod
    def reopen_checkpoint(project: Path, slug: str, cp: str) -> bool:
        """Make a checkpoint Claudo already consumed pending again, by removing it from the run state, so the
        next run re-enters it (and finds a `.rejected` file there). True if the state changed."""
        state = project / "work" / slug / ".runs" / "state.json"
        if not state.is_file():
            return False
        data = json.loads(state.read_text(encoding="utf-8"))
        if cp not in data:
            return False
        del data[cp]
        state.write_text(json.dumps(data), encoding="utf-8", newline="\n")
        return True


def state_dir() -> Path:
    """Per-user state that must live OUTSIDE every factory and app tree.

    Windows: %LOCALAPPDATA%/ai-factory. Elsewhere: $XDG_STATE_HOME/ai-factory (default
    ~/.local/state/ai-factory). AI_FACTORY_STATE_DIR overrides it (tests, containers)."""
    override = os.environ.get("AI_FACTORY_STATE_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "ai-factory"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "ai-factory"


def secret_path(root: Path) -> Path:
    """Where THIS factory's approval secret lives: per-user, one folder per factory root."""
    key = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:12]
    return state_dir() / key / "approval-secret"


def migrate_legacy_secret(root: Path) -> bool:
    """Move `<root>/.factory/approval-secret` (where earlier versions kept it, inside the tree the build
    agents work next to) to `secret_path`. Never overwrites an existing secret. True if a file was moved."""
    legacy = root / ".factory" / "approval-secret"
    if not legacy.is_file():
        return False
    target = secret_path(root)
    if not (target.is_file() and target.read_text(encoding="utf-8").strip()):
        _write_private(target, legacy.read_text(encoding="utf-8"))
    legacy.unlink()
    return True


def _write_private(path: Path, text: str) -> None:
    """Create `path` readable by this user only from its first byte (0600, in a 0700 directory), never
    world-readable for a moment under the default umask (audit A14, A15). On Windows the user profile's ACLs
    apply."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:  # pragma: no cover - a directory we cannot chmod (not ours): the file is still 0600
        pass
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def load_or_create_secret(path: Path) -> str:
    """The factory's approval-signing secret: created once, kept in the per-user state directory (outside
    the factory tree and every app) and withheld from the orchestrator's sub-agents' environment. A build
    agent running arbitrary code as this user could still read it (Claudo's residual M4: sandbox only)."""
    if path.is_file() and path.read_text(encoding="utf-8").strip():
        return path.read_text(encoding="utf-8").strip()
    secret = secrets.token_hex(32)
    _write_private(path, secret + "\n")
    return secret


# The orchestrator runs in its own process group, so stopping it stops its agents too (audit A12): killing
# only its PID left them running, and spending.
OWN_PROCESS_GROUP: dict = (
    {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
)


def _stop(proc: subprocess.Popen) -> None:
    """Stop the orchestrator AND every process it started (its agents, the docker CLIs)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=30)
    else:
        _signal_group(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            _signal_group(proc, signal.SIGKILL)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass
