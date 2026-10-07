"""Agent runners. `offline` = deterministic templates (free); `claude` = Claude Code CLI.

Same shape as Claudo's AgentRunner (lab/engine/runner.py) so the two can converge.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

READ_ONLY_TOOLS = ["Read", "Glob", "Grep"]
BUILD_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash(uv:*)", "Bash(uvx:*)", "Bash(python:*)"]


@dataclass(frozen=True)
class AgentResult:
    ok: bool
    text: str
    cost_usd: float = 0.0
    error: str = ""


class AgentError(Exception):
    pass


def resolve_claude() -> str | None:
    """Locate the Claude Code executable.

    On Windows `claude` is an npm .cmd shim; going through cmd.exe would mangle arguments
    such as `Bash(uv:*)`, so prefer the native claude.exe the shim points to."""
    found = shutil.which("claude")
    if found and found.lower().endswith((".cmd", ".ps1")):
        native = Path(found).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if native.is_file():
            return str(native)
    return found


def claude_argv(
    executable: str, *, model: str | None, tools: list[str], max_turns: int, permission_mode: str
) -> list[str]:
    """The prompt is NOT in argv: it goes through stdin (no quoting or length limits).

    An empty `tools` list means NO tools at all (`--tools ""`): for pure text-in/text-out calls such
    as the judge, which is both cheaper and rules out wandering through files."""
    argv = [
        executable,
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        permission_mode,
        "--max-turns",
        str(max_turns),
    ]
    argv += ["--allowedTools", ",".join(tools)] if tools else ["--tools", ""]
    if model:
        argv += ["--model", model]
    return argv


def parse_claude_json(returncode: int, stdout: str, stderr: str) -> AgentResult:
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        data = None
    if not isinstance(data, dict):
        if returncode != 0:
            return AgentResult(False, stdout, 0.0, (stderr or stdout)[-2000:])
        return AgentResult(True, stdout)
    cost = float(data.get("total_cost_usd") or 0.0)
    text = data.get("result") or ""
    if returncode != 0 or data.get("is_error"):
        return AgentResult(False, text, cost, (text or stderr or data.get("subtype", "error"))[-2000:])
    return AgentResult(True, text, cost)


class ClaudeRunner:
    name = "claude"

    def __init__(self, executable: str | None = None, timeout: int = 1800) -> None:
        found = executable or resolve_claude()
        if not found:
            raise AgentError("Claude Code CLI not found on PATH (install it, or use --runner offline)")
        self.executable = found
        self.timeout = timeout

    def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        model: str | None = None,
        tools: list[str] | None = None,
        max_turns: int = 10,
        permission_mode: str = "default",
        thinking_tokens: int | None = None,
    ) -> AgentResult:
        """`thinking_tokens` caps extended thinking (MAX_THINKING_TOKENS): thinking is billed as output,
        and on a judge call it was 93% of the cost (11k of 12k output tokens)."""
        argv = claude_argv(
            self.executable,
            model=model,
            tools=READ_ONLY_TOOLS if tools is None else tools,
            max_turns=max_turns,
            permission_mode=permission_mode,
        )
        try:
            p = subprocess.run(
                argv,
                cwd=cwd,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                env=None
                if thinking_tokens is None
                else {**os.environ, "MAX_THINKING_TOKENS": str(thinking_tokens)},
            )
        except subprocess.TimeoutExpired:
            return AgentResult(False, "", 0.0, f"claude timed out after {self.timeout}s")
        return parse_claude_json(p.returncode, p.stdout, p.stderr)


def get_runner(name: str) -> ClaudeRunner | None:
    """None means offline: stages fall back to their deterministic templates."""
    if name == "offline":
        return None
    if name == "claude":
        return ClaudeRunner()
    raise AgentError(f"unknown runner {name!r} (offline | claude)")


_FENCE = re.compile(r"^\s*```(?:markdown|md)?\s*\n(.*?)\n```\s*$", re.S)


def strip_fences(text: str) -> str:
    """Agents sometimes wrap a whole document in a code fence; unwrap it."""
    m = _FENCE.match(text)
    return (m.group(1) if m else text).strip() + "\n"
