"""Delivery to a git host: one PRIVATE repository per app, one pull request per change.

Safety by construction, not by configuration:
  - there is no visibility setting: the only way to create a repository is `--private`;
  - the factory never merges: it opens the pull request, IT merges it on the host;
  - it never pushes into a repository it did not create (an existing name is refused).
The host is an interface so tests exercise real git against a local bare repository instead of a network.
"""

from __future__ import annotations

import json
import subprocess
from typing import Protocol


class DeliveryError(Exception):
    pass


class GitHost(Protocol):
    def owner(self) -> str: ...

    def repo_exists(self, owner: str, name: str) -> bool: ...

    def create_private_repo(self, owner: str, name: str, description: str) -> str:
        """Create a PRIVATE empty repository and return the URL to push to."""
        ...

    def open_pull_request(self, owner: str, name: str, head: str, base: str, title: str, body: str) -> str:
        """Open a pull request and return its URL. Never merges."""
        ...

    def pull_request_state(self, url: str) -> str:
        """OPEN | MERGED | CLOSED."""
        ...


class GhCli:
    """GitHub through the `gh` CLI (already authenticated on this machine)."""

    def __init__(self, executable: str = "gh") -> None:
        self.executable = executable

    def _run(self, *args: str, input_text: str | None = None) -> str:
        try:
            p = subprocess.run(
                [self.executable, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                input=input_text,
                timeout=120,
            )
        except FileNotFoundError as e:
            raise DeliveryError("the GitHub CLI `gh` is not installed or not on PATH") from e
        except subprocess.TimeoutExpired as e:
            raise DeliveryError(f"gh {' '.join(args[:2])} timed out") from e
        if p.returncode != 0:
            raise DeliveryError(f"gh {' '.join(args[:2])} failed: {(p.stderr or p.stdout).strip()[-400:]}")
        return p.stdout.strip()

    def owner(self) -> str:
        return self._run("api", "user", "-q", ".login")

    def repo_exists(self, owner: str, name: str) -> bool:
        try:
            self._run("repo", "view", f"{owner}/{name}", "--json", "name")
        except DeliveryError as e:
            if "Could not resolve" in str(e) or "not found" in str(e).lower():
                return False
            raise
        return True

    def create_private_repo(self, owner: str, name: str, description: str) -> str:
        # `--private` is hardcoded on purpose: no code path can create a public repository.
        self._run("repo", "create", f"{owner}/{name}", "--private", "--description", description[:300])
        return f"https://github.com/{owner}/{name}.git"

    def open_pull_request(self, owner: str, name: str, head: str, base: str, title: str, body: str) -> str:
        return self._run(
            "pr", "create", "--repo", f"{owner}/{name}", "--head", head, "--base", base,
            "--title", title, "--body-file", "-", input_text=body,
        )  # fmt: skip

    def pull_request_state(self, url: str) -> str:
        return json.loads(self._run("pr", "view", url, "--json", "state"))["state"]
