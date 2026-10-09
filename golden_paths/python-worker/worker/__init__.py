"""{{title}}: event worker (IT golden path: python-worker).

The broker is not chosen here: the design (compiled from the tech radar) picks it, and an adapter implementing
`Inbox` connects it. Business logic lives in `handle`, tested without any broker.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Message:
    topic: str
    payload: dict = field(default_factory=dict)


class Inbox(Protocol):
    def receive(self) -> Message | None: ...

    def ack(self, message: Message) -> None: ...


class MemoryInbox:
    """An in-process inbox for tests and local runs."""

    def __init__(self, messages: list[Message] | None = None) -> None:
        self.pending: deque[Message] = deque(messages or [])
        self.acked: list[Message] = []

    def receive(self) -> Message | None:
        return self.pending.popleft() if self.pending else None

    def ack(self, message: Message) -> None:
        self.acked.append(message)


def handle(message: Message) -> dict:
    """The business logic for one message. Replace with the behaviour the spec describes."""
    return {"status": "ok", "topic": message.topic}


def run(inbox: Inbox, limit: int | None = None) -> int:
    """Consume until the inbox is empty (or `limit` messages). A message is acknowledged only after it was
    handled, so a crash redelivers it. Returns how many were processed."""
    processed = 0
    while limit is None or processed < limit:
        message = inbox.receive()
        if message is None:
            break
        handle(message)
        inbox.ack(message)
        processed += 1
    return processed
