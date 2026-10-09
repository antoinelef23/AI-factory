"""Who is acting: a verified identity and the roles IT gave it.

Without this, roles are self-declared: anyone can type `--as it --by Bob`, and the signed approval token
proves that the factory wrote it, not who decided. With `[identity] provider = "github"` the actor is the
account the `gh` CLI is authenticated as (`--by` can no longer claim someone else), and `roles.toml`,
maintained by IT like the radar, says which roles each account holds. `four_eyes` adds separation of duties:
one person cannot decide checkpoints for two different roles of the same item (e.g. approve the plan as
owner, then ship it as IT).
"""

from __future__ import annotations

import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

ROLE_NAMES = ("business", "it", "owner")


class IdentityError(Exception):
    pass


@dataclass(frozen=True)
class Identity:
    login: str
    source: str  # how it was verified: "github" (the authenticated gh account)


class IdentityProvider(Protocol):
    def current(self) -> Identity: ...


class GhIdentity:
    """The GitHub account the `gh` CLI is logged in as on this machine (`gh api user`)."""

    def __init__(self, executable: str = "gh") -> None:
        self.executable = executable
        self._cached: Identity | None = None

    def current(self) -> Identity:
        if self._cached is None:
            try:
                p = subprocess.run(
                    [self.executable, "api", "user", "-q", ".login"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                )
            except FileNotFoundError as e:
                raise IdentityError("identity needs the GitHub CLI `gh`, which is not on PATH") from e
            except subprocess.TimeoutExpired as e:
                raise IdentityError("`gh api user` timed out: cannot verify who is acting") from e
            login = p.stdout.strip()
            if p.returncode != 0 or not login:
                raise IdentityError(
                    "cannot verify who is acting: `gh api user` failed "
                    f"({(p.stderr or p.stdout).strip()[-200:]}); run `gh auth login`"
                )
            self._cached = Identity(login, "github")
        return self._cached


@dataclass
class RoleMap:
    """roles.toml: `[roles] it = ["alice"], owner = ["bob"], business = ["carol", "dave"]`."""

    members: dict[str, list[str]] = field(default_factory=dict)

    def roles_of(self, login: str) -> list[str]:
        who = login.lower()
        return [role for role, logins in self.members.items() if who in (x.lower() for x in logins)]

    def holds(self, login: str, role: str) -> bool:
        return role in self.roles_of(login)


def load_roles(path: Path) -> RoleMap:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise IdentityError(
            f"identity is on but {path} does not exist: IT must say who holds which role"
        ) from e
    except tomllib.TOMLDecodeError as e:
        raise IdentityError(f"{path}: {e}") from e
    raw = data.get("roles", {})
    problems = [f"unknown role {r!r}" for r in raw if r not in ROLE_NAMES]
    problems += [f"{r}: expected a list of logins" for r, v in raw.items() if not isinstance(v, list)]
    if problems:
        raise IdentityError(f"{path}: " + "; ".join(problems))
    return RoleMap({role: [str(x) for x in raw.get(role, [])] for role in ROLE_NAMES})
