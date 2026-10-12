"""Transport-level fault injection for the laboratory gateway.

Faults model what a real network does to the product, without touching its
code or mocking browser responses: a request may be delayed before reaching
the application (late response of a former pipeline), or the application may
fully execute a request while its response is withheld (a client timeout
whose effect is still unknown). The product application never knows.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import json
import re
import threading
from typing import Awaitable, Callable

MODES = ("delay_before", "delay_after", "status")

ASGIApp = Callable[..., Awaitable[None]]


class FaultError(ValueError):
    """An invalid fault rule was submitted."""


@dataclass
class FaultRule:
    """One injected transport condition.

    ``path`` is a regular expression matched against the whole request path.
    ``org`` selects which application receives the fault: ``lab`` (main
    organization), ``lab-other`` or ``*``. ``remaining`` limits how many
    requests are affected (``None`` means every matching request).
    """

    id: str
    mode: str
    path: str
    method: str = "*"
    seconds: float = 0.0
    status: int = 503
    org: str = "lab"
    remaining: int | None = None
    hits: int = 0
    # Matching requests to let through untouched before the fault applies
    # (e.g. 1 to skip a dry-run preview that precedes the real mutation).
    skip: int = 0

    def validate(self) -> None:
        if self.mode not in MODES:
            raise FaultError(f"mode must be one of {MODES}")
        if not self.id or not isinstance(self.id, str):
            raise FaultError("id is required")
        try:
            re.compile(self.path)
        except re.error as error:
            raise FaultError(f"path is not a valid regular expression: {error}") from error
        if not 0 <= float(self.seconds) <= 60:
            raise FaultError("seconds must be between 0 and 60")
        if not 400 <= int(self.status) <= 599:
            raise FaultError("status must be an HTTP error status")
        if self.remaining is not None and int(self.remaining) < 0:
            raise FaultError("remaining must be non-negative")
        if int(self.skip) < 0:
            raise FaultError("skip must be non-negative")

    def matches(self, org: str, method: str, path: str) -> bool:
        if self.org not in ("*", org):
            return False
        if self.method not in ("*", method.upper()):
            return False
        if self.remaining is not None and self.remaining <= 0:
            return False
        return re.fullmatch(self.path, path) is not None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class FaultRegistry:
    def __init__(self) -> None:
        self._rules: list[FaultRule] = []
        self._lock = threading.Lock()

    def replace(self, rules: list[FaultRule]) -> None:
        for rule in rules:
            rule.validate()
        with self._lock:
            self._rules = list(rules)

    def add(self, rule: FaultRule) -> None:
        rule.validate()
        with self._lock:
            self._rules = [existing for existing in self._rules if existing.id != rule.id] + [rule]

    def clear(self) -> None:
        with self._lock:
            self._rules = []

    def rules(self) -> list[FaultRule]:
        with self._lock:
            return list(self._rules)

    def take(self, org: str, method: str, path: str) -> FaultRule | None:
        """Return the first matching rule and account for one hit."""

        with self._lock:
            for rule in self._rules:
                if rule.matches(org, method, path):
                    if rule.skip > 0:
                        rule.skip -= 1
                        continue
                    rule.hits += 1
                    if rule.remaining is not None:
                        rule.remaining -= 1
                    return rule
        return None


async def apply_fault(rule: FaultRule, app: ASGIApp, scope: dict, receive, send) -> None:
    if rule.mode == "delay_before":
        await asyncio.sleep(rule.seconds)
        await app(scope, receive, send)
        return
    if rule.mode == "delay_after":
        buffered: list[dict] = []

        async def capture(message: dict) -> None:
            buffered.append(message)

        # The application fully executes the request, effects included.
        await app(scope, receive, capture)
        await asyncio.sleep(rule.seconds)
        for message in buffered:
            try:
                await send(message)
            except Exception:  # the client already gave up: nothing left to deliver
                return
        return
    body = json.dumps(
        {
            "error": {
                "code": "store_unavailable",
                "message": "Service unavailable (laboratory-injected condition).",
                "next_action": "retry, alert the admin",
                "retryable": True,
            }
        }
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": int(rule.status),
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii"))],
        }
    )
    await send({"type": "http.response.body", "body": body})
