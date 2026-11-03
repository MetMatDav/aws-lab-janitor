"""Deployment configuration read from CDK context."""

import os
from dataclasses import dataclass
from typing import Any

from constructs import Node

DEFAULT_MIN_AGE_DAYS = 7
DEFAULT_SCHEDULE_TIMEZONE = "America/Bogota"
DEFAULT_SCHEDULE_HOUR = 9
_FALSY_FLAGS = ("false", "False")


def _context(node: Node, key: str, default: Any) -> Any:
    """Return the context value for ``key``, or ``default`` when it is unset."""
    value = node.try_get_context(key)
    return default if value is None else value


def _flag(value: Any, *, default: bool) -> bool:
    """Parse a boolean context value.

    Context passed with ``-c key=value`` arrives as a string, while context
    set in ``cdk.json`` or a test ``App`` can be a real boolean. Only the
    explicit falsy forms disable a flag; anything else keeps it on, so a typo
    never turns off a safety setting such as ``dryRun``.
    """
    if value is None:
        return default
    return not (value is False or value in _FALSY_FLAGS)


@dataclass(frozen=True)
class JanitorConfig:
    """Settings that shape the deployed janitor."""

    report_email: str
    alert_email: str
    dry_run: bool
    min_age_days: int
    schedule_timezone: str
    schedule_hour: int
    janitor_enabled: bool

    @classmethod
    def from_node(cls, node: Node) -> "JanitorConfig":
        """Build the configuration from the context visible to ``node``."""
        report_email = node.try_get_context("reportEmail") or os.environ.get("JANITOR_REPORT_EMAIL")
        if not report_email:
            raise ValueError("Set -c reportEmail=you@example.com")

        return cls(
            report_email=report_email,
            alert_email=node.try_get_context("alertEmail") or report_email,
            dry_run=_flag(node.try_get_context("dryRun"), default=True),
            min_age_days=int(_context(node, "minAgeDays", DEFAULT_MIN_AGE_DAYS)),
            schedule_timezone=_context(node, "scheduleTimezone", DEFAULT_SCHEDULE_TIMEZONE),
            schedule_hour=int(_context(node, "scheduleHour", DEFAULT_SCHEDULE_HOUR)),
            janitor_enabled=_flag(node.try_get_context("janitorEnabled"), default=True),
        )
