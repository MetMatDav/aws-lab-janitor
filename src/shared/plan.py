"""The immutable deletion plan: a canonical JSON document identified by its sha256."""

import hashlib
import json
import uuid
from datetime import UTC, datetime

from shared.findings import ErrorFinding, Finding
from shared.pricing import PRICES_AS_OF, PRICES_REGION
from shared.settings import Settings

RUN_ID_TIME_FORMAT = "%Y%m%dT%H%M%SZ"


def new_run_id(now: datetime) -> str:
    """Run id ``YYYYMMDDTHHMMSSZ-<4 hex>`` for ``now`` in UTC; safe in S3 keys and record ids."""
    return f"{now.astimezone(UTC).strftime(RUN_ID_TIME_FORMAT)}-{uuid.uuid4().hex[:4]}"


def _finding_key(finding: Finding) -> tuple[str, str, str]:
    return (finding.region, finding.kind, finding.resource_id)


def _error_key(error: ErrorFinding) -> tuple[str, str, str, str]:
    return (error.region, error.service, error.classification, error.error)


# The six inputs are the plan's contract (fixed by the detector handler that calls it).
def build_plan(  # noqa: PLR0913, PLR0917
    run_id: str,
    settings: Settings,
    findings: list[Finding],
    errors: list[ErrorFinding],
    regions: list[str],
    generated_at: datetime,
) -> dict:
    """Assemble the plan document for one detection run.

    Candidates, inventory and errors are sorted so that the same inputs always
    produce the same bytes (and therefore the same sha256), whatever the order
    in which the detectors reported them.
    """
    ordered = sorted(findings, key=_finding_key)
    costs = [f.estimated_monthly_usd for f in findings if f.estimated_monthly_usd is not None]
    return {
        "runId": run_id,
        "generatedAt": generated_at.astimezone(UTC).isoformat(),
        "dryRun": settings.dry_run,
        "criteria": {
            "tagKey": settings.tag_key,
            "tagValue": settings.tag_value,
            "namePrefix": settings.name_prefix,
            "keepTag": f"{settings.keep_tag_key}={settings.keep_tag_value}",
            "minAgeDays": settings.min_age_days,
        },
        "regions": list(regions),
        "candidates": [f.to_dict() for f in ordered if f.candidate],
        "inventory": [f.to_dict() for f in ordered if not f.candidate],
        "errors": [e.to_dict() for e in sorted(errors, key=_error_key)],
        "estimatedMonthlyUsd": round(sum(costs), 2),
        "pricesAsOf": PRICES_AS_OF,
        "pricesRegion": PRICES_REGION,
    }


def plan_bytes(plan: dict) -> bytes:
    """Canonical serialization: sorted keys, no insignificant whitespace, UTF-8."""
    return json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()


def plan_sha256(data: bytes) -> str:
    """Hex sha256 of serialized plan bytes."""
    return hashlib.sha256(data).hexdigest()
