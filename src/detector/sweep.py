"""Detection framework: per-call error isolation, error classification and pagination."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from botocore.exceptions import ClientError, EndpointConnectionError, UnknownServiceError

from shared.aws_clients import ClientFactory
from shared.findings import ErrorFinding
from shared.settings import Settings

ACCESS_DENIED_CODES = frozenset(
    {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "OptInRequired"}
)
THROTTLED_CODES = frozenset(
    {"Throttling", "ThrottlingException", "RequestLimitExceeded", "TooManyRequestsException"}
)
UNAVAILABLE_CODES = frozenset({"InvalidAction"})
HTTP_NOT_FOUND = 404
MAX_ERROR_LENGTH = 160
TIME_BUDGET_MESSAGE = "skipped: time budget exhausted"


class TimeBudgetExceeded(Exception):
    """Raised (or recorded) when the sweep runs out of its time budget."""


@dataclass
class SweepContext:
    """Everything a detector needs besides the region it inspects."""

    settings: Settings
    clients: ClientFactory
    now: datetime
    account: str
    was_proposed_live: Callable[[str], bool]
    deadline: float
    log: logging.LoggerAdapter


def classify_error(exc: BaseException) -> str:
    """Map an exception to the error classification reported for a service/region."""
    if isinstance(exc, TimeBudgetExceeded):
        return "time-budget"
    if isinstance(exc, EndpointConnectionError | UnknownServiceError):
        return "service-unavailable-in-region"
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in ACCESS_DENIED_CODES:
            return "access-denied"
        if code in THROTTLED_CODES:
            return "throttled"
        if status == HTTP_NOT_FOUND or code in UNAVAILABLE_CODES:
            return "service-unavailable-in-region"
    return "other"


def safe(
    ctx: SweepContext,
    errors: list[ErrorFinding],
    region: str,
    service: str,
    fn: Callable[[], None],
) -> None:
    """Run ``fn``; on failure record an ErrorFinding and log a warning. Never raises.

    When the time budget is already exhausted ``fn`` is not called at all and a
    ``time-budget`` error is recorded instead.
    """
    try:
        if time.time() > ctx.deadline:
            raise TimeBudgetExceeded(TIME_BUDGET_MESSAGE)
        fn()
    # One failing call must never stop the sweep, whatever the exception type.
    except Exception as exc:  # noqa: BLE001
        error = ErrorFinding(region, service, str(exc)[:MAX_ERROR_LENGTH], classify_error(exc))
        errors.append(error)
        ctx.log.warning(
            "%s in %s failed (%s): %s", service, region, error.classification, error.error
        )


def collect(client, op: str, key: str, **params) -> list:
    """Return every item under ``key`` for ``op``, following pagination when available.

    ``key`` may be a dotted path such as ``DistributionList.Items``; a missing
    key yields no items for that page.
    """
    if client.can_paginate(op):
        pages = client.get_paginator(op).paginate(**params)
    else:
        pages = [getattr(client, op)(**params)]
    items: list = []
    for page in pages:
        items.extend(_dig(page, key))
    return items


def _dig(data: dict, path: str) -> list:
    """Resolve a dotted ``path`` inside ``data``; missing segments resolve to an empty list."""
    value = data
    for part in path.split("."):
        if not isinstance(value, dict):
            return []
        value = value.get(part)
    return value or []
