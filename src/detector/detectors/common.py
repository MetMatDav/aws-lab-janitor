"""Helpers shared by every detector module: Finding construction, tag lookups, keep blockers."""

from collections.abc import Callable

from detector.sweep import SweepContext, safe
from shared.findings import ErrorFinding, Finding
from shared.marking import is_kept
from shared.settings import Settings


def make_finding(region: str, kind: str, resource_id: str, lab: bool, **fields) -> Finding:
    """Build a Finding; ``fields`` carries ``detail`` plus any non-default field.

    Defaults: no ARN, no tags, not a candidate, empty reason, unknown age and cost.
    """
    defaults = {
        "arn": None,
        "tags": [],
        "candidate": False,
        "reason": "",
        "age_days": None,
        "estimated_monthly_usd": None,
    }
    return Finding(
        region=region, kind=kind, resource_id=resource_id, lab=lab, **(defaults | fields)
    )


def first_blocker(tags: list[dict], settings: Settings, *checks: str | None) -> str | None:
    """First reason that blocks a lab resource: the keep tag, then ``checks`` in order."""
    if is_kept(tags, settings):
        return f"tagged {settings.keep_tag_key}={settings.keep_tag_value}"
    return next((check for check in checks if check), None)


def read_tags(
    ctx: SweepContext,
    errors: list[ErrorFinding],
    region: str,
    service: str,
    fetch: Callable[[], list[dict]],
) -> list[dict] | None:
    """Run one tag lookup in its own ``safe`` block; None when it failed (the error is recorded).

    Callers decide what a failure means: ``or []`` falls back to prefix-only marking, while
    candidate decisions treat None as unknown tags and fail closed.
    """
    tags: list[dict] | None = None

    def run() -> None:
        nonlocal tags
        tags = list(fetch())

    safe(ctx, errors, region, service, run)
    return tags
