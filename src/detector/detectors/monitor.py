"""Internet Monitor detector: monitors named with the lab prefix are candidates unless kept.

Fail closed: a monitor is a candidate only when its name carries the lab prefix and its tags
were read successfully without the keep tag.
"""

from detector.detectors.common import first_blocker, make_finding
from detector.sweep import SweepContext, collect, safe
from shared.findings import ErrorFinding, Finding

TAGS_UNKNOWN = "tags unknown"


def detect_internet_monitor(
    region: str, ctx: SweepContext, findings: list[Finding], errors: list[ErrorFinding]
) -> None:
    """Inspect Internet Monitor monitors in ``region``; only lab-prefixed ones get a tag lookup."""

    def internetmonitor():
        # Fetched inside each safe block so a client-creation failure is recorded, not raised.
        return ctx.clients.get("internetmonitor", region)

    def monitor_tags(arn: str) -> list[dict] | None:
        """Tags as ``{"Key", "Value"}`` pairs, or None when the lookup failed."""
        tags: list[dict] = []
        fetched = False

        def fetch() -> None:
            nonlocal fetched
            response = internetmonitor().list_tags_for_resource(ResourceArn=arn)
            tags.extend({"Key": k, "Value": v} for k, v in response.get("Tags", {}).items())
            fetched = True

        safe(ctx, errors, region, "internetmonitor:ListTagsForResource", fetch)
        return tags if fetched else None

    def run_monitors() -> None:
        for monitor in collect(
            internetmonitor(), "list_monitors", "Monitors", deadline=ctx.deadline
        ):
            lab = monitor["MonitorName"].startswith(ctx.settings.name_prefix)
            tags = monitor_tags(monitor["MonitorArn"]) if lab else []
            findings.append(_monitor(monitor, lab, tags, region, ctx))

    safe(ctx, errors, region, "internetmonitor:ListMonitors", run_monitors)


def _monitor(
    monitor: dict, lab: bool, tags: list[dict] | None, region: str, ctx: SweepContext
) -> Finding:
    """Monitor verdict; ``tags`` is None when the tag lookup of a lab monitor failed."""
    settings = ctx.settings
    candidate, reason = False, ""
    if lab:
        blocker = TAGS_UNKNOWN if tags is None else first_blocker(tags, settings)
        candidate = blocker is None
        reason = f"name prefix {settings.name_prefix}" if candidate else blocker or ""
    return make_finding(
        region,
        "internet-monitor",
        monitor["MonitorName"],
        lab,
        arn=monitor.get("MonitorArn"),
        tags=tags or [],
        detail=monitor.get("Status", ""),
        candidate=candidate,
        reason=reason,
    )
