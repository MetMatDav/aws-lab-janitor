"""Deployment configuration read from CDK context.

Context keys: ``reportEmail`` (required, or ``JANITOR_REPORT_EMAIL``),
``alertEmail``, ``approvalEmail``, ``dryRun``, ``minAgeDays``,
``scheduleTimezone``, ``scheduleHour``, ``janitorEnabled``,
``reservedConcurrency``, ``allowedRegions``, ``approvalAuth``,
``approverRoleArn`` and ``createTrail``.
"""

import os
import re
from dataclasses import dataclass
from typing import Any

from constructs import Node

DEFAULT_MIN_AGE_DAYS = 7
# Ten years. Beyond the calendar the detector's date arithmetic overflows; the
# Lambdas apply the same cap to the execution input (shared/settings.py).
MAX_MIN_AGE_DAYS = 3650
DEFAULT_SCHEDULE_TIMEZONE = "America/Bogota"
DEFAULT_SCHEDULE_HOUR = 9
_TRUE_FLAGS = (True, "true", "True")
_FALSE_FLAGS = (False, "false", "False")
MAX_SCHEDULE_HOUR = 23
APPROVAL_AUTH_MODES = ("link", "iam", "both")
DEFAULT_APPROVAL_AUTH = "link"
CREATE_TRAIL_MODES = ("full", "dataEventsOnly", "none")
DEFAULT_CREATE_TRAIL = "full"
# The executor reaches global services (CloudWatch dashboards) through this region.
GLOBAL_SERVICES_REGION = "us-east-1"
# Matched with fullmatch and re.ASCII: "$" would accept a trailing newline, and a
# bare \d would accept non-ASCII digits.
_REGION = re.compile(r"[a-z]{2}(-[a-z]+)+-\d", re.ASCII)
# One role: an optional path and a name, in the characters IAM allows for them.
_ROLE_ARN = re.compile(
    r"arn:aws(-[a-z]+)*:iam::\d{12}:role/([A-Za-z0-9_+=,.@-]+/)*[A-Za-z0-9_+=,.@-]+", re.ASCII
)
# One plain address. Stricter than the RFC on purpose: quotes, commas and angle
# brackets pass a looser check and then fail at deploy, or name a second recipient.
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+", re.ASCII)
# Bounded: int() itself refuses strings of thousands of digits with another message.
_INTEGER = re.compile(r"-?[0-9]{1,9}", re.ASCII)


def _context(node: Node, key: str, default: Any) -> Any:
    """Return the context value for ``key``, or ``default`` when it is unset."""
    value = node.try_get_context(key)
    return default if value is None else value


def _int_in_range(key: str, value: Any, low: int, high: int | None = None) -> int:
    """Parse an integer context value and check ``low <= value <= high``.

    Context passed with ``-c key=value`` arrives as a string, so strings of
    ASCII digits are accepted; booleans, floats, anything ``int()`` would
    forgive (other digits, ``1_0``, spaces, a sign) and anything out of range
    are not.
    """
    expected = f">= {low}" if high is None else f"in {low}..{high}"
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ValueError(f"{key} must be an integer {expected}, got {value!r}")
    if isinstance(value, str) and not _INTEGER.fullmatch(value):
        raise ValueError(f"{key} must be an integer {expected}, got {value!r}")
    number = int(value)
    if number < low or (high is not None and number > high):
        raise ValueError(f"{key} must be an integer {expected}, got {value!r}")
    return number


def _flag(key: str, value: Any, *, default: bool) -> bool:
    """Parse a boolean context value.

    Context passed with ``-c key=value`` arrives as a string, while context
    set in ``cdk.json`` or a test ``App`` can be a real boolean. Anything else
    is an error: guessing would arm deletions on a mistyped ``dryRun`` or
    leave the schedule running on a mistyped ``janitorEnabled``.
    """
    if value is None:
        return default
    # ``0 == False`` in Python: compare the type too.
    if isinstance(value, bool | str):
        if value in _TRUE_FLAGS:
            return True
        if value in _FALSE_FLAGS:
            return False
    raise ValueError(f"{key} must be true or false, got {value!r}")


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
        if not _REGION.fullmatch(region):
            raise ValueError(f"allowedRegions has an invalid region name: {region!r}")
    if regions and GLOBAL_SERVICES_REGION not in regions:
        raise ValueError(
            f"allowedRegions must include {GLOBAL_SERVICES_REGION}: "
            "CloudWatch dashboards are deleted through it"
        )
    return regions


def _email(key: str, value: Any) -> str:
    """Check an email context value has the shape ``local@domain.tld``.

    SNS rejects a malformed subscription endpoint only at deploy time, after
    CloudFormation has started the update, so a placeholder left in the
    deploy command (``-c reportEmail=TU_CORREO``) would cost a full rollback.
    Failing here keeps the mistake inside ``cdk synth``.
    """
    if not isinstance(value, str) or not _EMAIL.fullmatch(value):
        raise ValueError(f"{key} must be an email address such as you@example.com, got {value!r}")
    return value


def _approval_auth(value: Any) -> str:
    """Check ``approvalAuth`` is one of the supported modes."""
    if value not in APPROVAL_AUTH_MODES:
        raise ValueError(
            f"approvalAuth must be one of {', '.join(APPROVAL_AUTH_MODES)}, got {value!r}"
        )
    return value


def _create_trail(value: Any) -> str:
    """Check ``createTrail`` is one of the supported modes."""
    if value not in CREATE_TRAIL_MODES:
        raise ValueError(
            f"createTrail must be one of {', '.join(CREATE_TRAIL_MODES)}, got {value!r}"
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
    if not isinstance(value, str) or not _ROLE_ARN.fullmatch(value):
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
    create_trail: str

    @classmethod
    def from_node(cls, node: Node) -> "JanitorConfig":
        """Build the configuration from the context visible to ``node``."""
        report_email = node.try_get_context("reportEmail") or os.environ.get("JANITOR_REPORT_EMAIL")
        if not report_email:
            raise ValueError("Set -c reportEmail=you@example.com")

        approval_auth = _approval_auth(_context(node, "approvalAuth", DEFAULT_APPROVAL_AUTH))
        return cls(
            report_email=_email("reportEmail", report_email),
            alert_email=_email("alertEmail", node.try_get_context("alertEmail") or report_email),
            approval_email=_email(
                "approvalEmail", node.try_get_context("approvalEmail") or report_email
            ),
            dry_run=_flag("dryRun", node.try_get_context("dryRun"), default=True),
            min_age_days=_int_in_range(
                "minAgeDays",
                _context(node, "minAgeDays", DEFAULT_MIN_AGE_DAYS),
                0,
                MAX_MIN_AGE_DAYS,
            ),
            schedule_timezone=_context(node, "scheduleTimezone", DEFAULT_SCHEDULE_TIMEZONE),
            schedule_hour=_int_in_range(
                "scheduleHour",
                _context(node, "scheduleHour", DEFAULT_SCHEDULE_HOUR),
                0,
                MAX_SCHEDULE_HOUR,
            ),
            janitor_enabled=_flag(
                "janitorEnabled", node.try_get_context("janitorEnabled"), default=True
            ),
            reserved_concurrency=_flag(
                "reservedConcurrency", node.try_get_context("reservedConcurrency"), default=True
            ),
            allowed_regions=_regions(node.try_get_context("allowedRegions")),
            approval_auth=approval_auth,
            approver_role_arn=_approver_role_arn(
                node.try_get_context("approverRoleArn"), approval_auth
            ),
            create_trail=_create_trail(_context(node, "createTrail", DEFAULT_CREATE_TRAIL)),
        )
