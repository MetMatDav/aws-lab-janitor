"""Deployment configuration read from CDK context.

Context keys: ``reportEmail`` (required, or ``JANITOR_REPORT_EMAIL``),
``alertEmail``, ``approvalEmail``, ``dryRun``, ``minAgeDays``,
``scheduleTimezone``, ``scheduleHour``, ``janitorEnabled``,
``reservedConcurrency``, ``allowedRegions``, ``approvalAuth`` and
``approverRoleArn``.
"""

import os
import re
from dataclasses import dataclass
from typing import Any

from constructs import Node

DEFAULT_MIN_AGE_DAYS = 7
DEFAULT_SCHEDULE_TIMEZONE = "America/Bogota"
DEFAULT_SCHEDULE_HOUR = 9
_FALSY_FLAGS = ("false", "False")
MAX_SCHEDULE_HOUR = 23
APPROVAL_AUTH_MODES = ("link", "iam", "both")
DEFAULT_APPROVAL_AUTH = "link"
_REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
_ROLE_ARN = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:role/.+$")


def _context(node: Node, key: str, default: Any) -> Any:
    """Return the context value for ``key``, or ``default`` when it is unset."""
    value = node.try_get_context(key)
    return default if value is None else value


def _int_in_range(key: str, value: Any, low: int, high: int | None = None) -> int:
    """Parse an integer context value and check ``low <= value <= high``.

    Context passed with ``-c key=value`` arrives as a string, so decimal digit
    strings are accepted; booleans, floats and anything out of range are not.
    """
    expected = f">= {low}" if high is None else f"in {low}..{high}"
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ValueError(f"{key} must be an integer {expected}, got {value!r}")
    try:
        number = int(value)
    except ValueError:
        raise ValueError(f"{key} must be an integer {expected}, got {value!r}") from None
    if number < low or (high is not None and number > high):
        raise ValueError(f"{key} must be an integer {expected}, got {value!r}")
    return number


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


def _regions(value: Any) -> tuple[str, ...]:
    """Parse ``allowedRegions``: a comma-separated string or a list of names.

    Whitespace is stripped and empty entries are dropped; an empty result
    means every region.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        entries = value.split(",")
    elif isinstance(value, list | tuple) and all(isinstance(entry, str) for entry in value):
        entries = list(value)
    else:
        raise ValueError(f"allowedRegions must be a comma-separated string or list, got {value!r}")
    regions = tuple(entry.strip() for entry in entries if entry.strip())
    for region in regions:
        if not _REGION.match(region):
            raise ValueError(f"allowedRegions has an invalid region name: {region!r}")
    return regions


def _approval_auth(value: Any) -> str:
    """Check ``approvalAuth`` is one of the supported modes."""
    if value not in APPROVAL_AUTH_MODES:
        raise ValueError(
            f"approvalAuth must be one of {', '.join(APPROVAL_AUTH_MODES)}, got {value!r}"
        )
    return value


def _approver_role_arn(value: Any, approval_auth: str) -> str | None:
    """Check ``approverRoleArn``: required by the ``iam`` and ``both`` modes.

    A value set in ``link`` mode is validated too, so a typo is not kept
    silently until the mode changes.
    """
    if value is None:
        if approval_auth != "link":
            raise ValueError(f"approvalAuth={approval_auth} needs -c approverRoleArn=<role ARN>")
        return None
    if not isinstance(value, str) or not _ROLE_ARN.match(value):
        raise ValueError(f"approverRoleArn must be an IAM role ARN, got {value!r}")
    return value


@dataclass(frozen=True)
class JanitorConfig:
    """Settings that shape the deployed janitor."""

    report_email: str
    alert_email: str
    approval_email: str
    dry_run: bool
    min_age_days: int
    schedule_timezone: str
    schedule_hour: int
    janitor_enabled: bool
    reserved_concurrency: bool
    allowed_regions: tuple[str, ...]
    approval_auth: str
    approver_role_arn: str | None

    @classmethod
    def from_node(cls, node: Node) -> "JanitorConfig":
        """Build the configuration from the context visible to ``node``."""
        report_email = node.try_get_context("reportEmail") or os.environ.get("JANITOR_REPORT_EMAIL")
        if not report_email:
            raise ValueError("Set -c reportEmail=you@example.com")

        approval_auth = _approval_auth(_context(node, "approvalAuth", DEFAULT_APPROVAL_AUTH))
        return cls(
            report_email=report_email,
            alert_email=node.try_get_context("alertEmail") or report_email,
            approval_email=node.try_get_context("approvalEmail") or report_email,
            dry_run=_flag(node.try_get_context("dryRun"), default=True),
            min_age_days=_int_in_range(
                "minAgeDays", _context(node, "minAgeDays", DEFAULT_MIN_AGE_DAYS), 0
            ),
            schedule_timezone=_context(node, "scheduleTimezone", DEFAULT_SCHEDULE_TIMEZONE),
            schedule_hour=_int_in_range(
                "scheduleHour",
                _context(node, "scheduleHour", DEFAULT_SCHEDULE_HOUR),
                0,
                MAX_SCHEDULE_HOUR,
            ),
            janitor_enabled=_flag(node.try_get_context("janitorEnabled"), default=True),
            reserved_concurrency=_flag(node.try_get_context("reservedConcurrency"), default=True),
            allowed_regions=_regions(node.try_get_context("allowedRegions")),
            approval_auth=approval_auth,
            approver_role_arn=_approver_role_arn(
                node.try_get_context("approverRoleArn"), approval_auth
            ),
        )
