"""Builds run sandboxed by default (ROADMAP P1-7).

An agent that writes and runs code has code execution as this user: on the host it could read the approval
secret and forge a signed checkpoint. So whenever an agent BUILDS, its tool calls, Claudo's per-task verify
and evals, and the factory's own test/lint gates all run in Claudo's hardened container
(`deploy/sandbox/Dockerfile.agent`): only the app is mounted, the rootfs is read-only, every capability is
dropped, the approval secret never enters, and the network is internal with an allowlist proxy as the only
way out (model API and package registries). Claudo's `deploy/sandbox/setup.sh` builds all of it once.

The host runs agent code only with an explicit `--unsafe-host`, recorded on the item for IT to acknowledge.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from factory.agents import AgentResult, claude_argv, parse_claude_json

# The agent's own model credential (distinct from the approval secret, which never enters the container).
CREDENTIALS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
FORWARDED = frozenset({*CREDENTIALS, "ANTHROPIC_BASE_URL", "LANG", "LC_ALL", "TZ", "NO_COLOR"})
HOME = "/home/agent"

# (argv) -> (returncode, stdout). Injectable: tests never need Docker.
Docker = Callable[[list[str]], tuple[int, str]]


def docker_cli(args: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30
        )
    except FileNotFoundError:
        return 127, "docker not found"
    except subprocess.TimeoutExpired:
        return 124, "docker did not answer within 30s"
    return p.returncode, (p.stdout + p.stderr).strip()


@dataclass
class Sandbox:
    image: str = "lab-agent:latest"
    network: str = "lab-egress"
    proxy: str = "http://lab-egress-proxy:8888"
    proxy_container: str = "lab-egress-proxy"
    memory: str = "4g"
    pids: int = 1024
    timeout: int = 1800
    docker: Docker = docker_cli
    # From a Windows host the bind mount shows every file as 0777 (chmod is a no-op): checks then run through
    # the image's `lab-ws`, on a copy carrying git's file modes (else ruff's EXE rules flag every file).
    git_modes: bool = os.name == "nt"

    # ------------------------------------------------------------------ readiness
    def problems(self, *, need_credential: bool = True) -> list[str]:
        """Empty when a sandboxed build can start; otherwise what to fix, in order."""
        rc, out = self.docker(["info", "--format", "{{.ServerVersion}}"])
        if rc != 0:
            return [f"Docker is not running ({out[-200:] or rc}): start Docker Desktop"]
        found = []
        if self.docker(["image", "inspect", self.image, "--format", "{{.Id}}"])[0] != 0:
            found.append(f"image {self.image} is missing")
        rc, internal = self.docker(["network", "inspect", self.network, "--format", "{{.Internal}}"])
        if rc != 0:
            found.append(f"network {self.network} is missing")
        elif internal.strip() != "true":
            found.append(f"network {self.network} is not internal: agents would have a direct route out")
        rc, running = self.docker(["inspect", self.proxy_container, "--format", "{{.State.Running}}"])
        if rc != 0 or running.strip() != "true":
            found.append(f"the egress proxy container {self.proxy_container} is not running")
        if found:
            found.append("run Claudo's `bash deploy/sandbox/setup.sh` once to create them")
        if need_credential and not any(os.environ.get(k) for k in CREDENTIALS):
            found.append(
                "no model credential for the agent in the container: set CLAUDE_CODE_OAUTH_TOKEN "
                "(`claude setup-token`) or ANTHROPIC_API_KEY"
            )
        return found

    # ------------------------------------------------------------------ the container
    def env(self) -> dict[str, str]:
        out = {k: v for k, v in os.environ.items() if k in FORWARDED and v}
        out.update(
            {
                "HTTPS_PROXY": self.proxy,
                "HTTP_PROXY": self.proxy,
                "https_proxy": self.proxy,
                "http_proxy": self.proxy,
                "NO_PROXY": "localhost,127.0.0.1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            }
        )
        return out

    def argv(self, workspace: Path, command: list[str], extra_env: dict[str, str] | None = None) -> list[str]:
        """The hardened `docker run`, the same confinement as Claudo's sandbox runner.

        The app's .git is mounted read-only on top: the host runs git in this folder afterwards, so code in
        the container must not be able to plant a hook or a config setting there (audit A1). Agents never
        commit: the factory and Claudo's orchestrator commit on the host."""
        ws = Path(workspace).resolve()
        argv = [
            "docker", "run", "--rm", "-i",
            "--network", self.network,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,nodev",
            "--tmpfs", f"{HOME}:rw,exec,nosuid,nodev",  # docker's tmpfs default is noexec: caches run here
            "--pids-limit", str(self.pids),
            "--memory", self.memory,
            "-v", f"{ws.as_posix()}:/workspace:rw",
        ]  # fmt: skip
        if (ws / ".git").exists():
            argv += ["-v", f"{(ws / '.git').as_posix()}:/workspace/.git:ro"]
        argv += ["-w", "/workspace", "-e", f"HOME={HOME}"]
        for key, value in sorted({**self.env(), **(extra_env or {})}.items()):
            argv += ["-e", f"{key}={value}"]
        return [*argv, self.image, *command]

    def claudo_env(self) -> dict[str, str]:
        """What makes Claudo run its agents AND their code (verify, evals) in this sandbox."""
        return {
            "LAB_RUNNER": "sandbox",
            "LAB_SANDBOX_IMAGE": self.image,
            "LAB_SANDBOX_NETWORK": self.network,
            "LAB_SANDBOX_PROXY": self.proxy,
            "LAB_SANDBOX_MEMORY": self.memory,
        }

    def executor(self, command: str, cwd: Path) -> tuple[int, str]:
        """A gate command (`uv run pytest`, `npm ci`...) in the container. The environment goes to
        /workspace/.venv so the dependencies gate can read the installed licences afterwards."""
        shell = ["lab-ws", "sh", "-c", command] if self.git_modes else ["sh", "-c", command]
        argv = self.argv(cwd, shell, {"UV_PROJECT_ENVIRONMENT": "/workspace/.venv"})
        try:
            p = subprocess.run(
                argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=self.timeout
            )
        except subprocess.TimeoutExpired:
            return 124, f"timeout: {command}"
        except FileNotFoundError:
            return 127, "docker not found: the sandbox needs Docker"
        return p.returncode, (p.stdout + p.stderr)[-4000:]


class SandboxedRunner:
    """The factory's own build agent (targeted fixes, builds without Claudo) in the container."""

    name = "claude-sandbox"

    def __init__(self, sandbox: Sandbox, timeout: int = 1800) -> None:
        self.sandbox = sandbox
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
        command = claude_argv(
            "claude", model=model, tools=tools or [], max_turns=max_turns, permission_mode=permission_mode
        )
        extra = {} if thinking_tokens is None else {"MAX_THINKING_TOKENS": str(thinking_tokens)}
        try:
            p = subprocess.run(
                self.sandbox.argv(cwd, command, extra),
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            return AgentResult(False, "", 0.0, f"sandboxed claude timed out after {self.timeout}s")
        except FileNotFoundError:
            return AgentResult(False, "", 0.0, "docker not found: the sandbox needs Docker")
        return parse_claude_json(p.returncode, p.stdout, p.stderr)
