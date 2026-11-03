"""Elastic Load Balancing v2 detector: application, network and gateway load balancers.

Load balancers are reported with their fixed hourly price; none of them is ever a candidate.
"""

from detector.detectors.common import make_finding
from detector.sweep import SweepContext, collect, safe
from shared.findings import ErrorFinding, Finding
from shared.marking import age_days, is_lab
from shared.pricing import monthly_usd

# DescribeTags accepts at most 20 resource ARNs per call.
TAG_BATCH_SIZE = 20
PRICE_KIND_BY_TYPE = {"application": "alb", "network": "nlb"}


def detect_elbv2(
    region: str, ctx: SweepContext, findings: list[Finding], errors: list[ErrorFinding]
) -> None:
    """Inspect ELBv2 load balancers in ``region``; each tag batch has its own ``safe`` block."""

    def elbv2():
        # Fetched inside each safe block so a client-creation failure is recorded, not raised.
        return ctx.clients.get("elbv2", region)

    def batch_tags(arns: list[str]) -> dict[str, list[dict]]:
        """Tags by ARN for one batch; empty when the call fails (marking falls back to prefix)."""
        tags: dict[str, list[dict]] = {}

        def fetch() -> None:
            response = elbv2().describe_tags(ResourceArns=arns)
            for description in response.get("TagDescriptions", []):
                tags[description["ResourceArn"]] = description.get("Tags", [])

        safe(ctx, errors, region, "elasticloadbalancing:DescribeTags", fetch)
        return tags

    def run_load_balancers() -> None:
        load_balancers = collect(elbv2(), "describe_load_balancers", "LoadBalancers")
        arns = [lb["LoadBalancerArn"] for lb in load_balancers]
        tags: dict[str, list[dict]] = {}
        for start in range(0, len(arns), TAG_BATCH_SIZE):
            tags |= batch_tags(arns[start : start + TAG_BATCH_SIZE])
        for lb in load_balancers:
            findings.append(_load_balancer(lb, tags.get(lb["LoadBalancerArn"], []), region, ctx))

    safe(ctx, errors, region, "elasticloadbalancing:DescribeLoadBalancers", run_load_balancers)


def _load_balancer(lb: dict, tags: list[dict], region: str, ctx: SweepContext) -> Finding:
    """Load balancer priced by type (gateway load balancers have no fixed price)."""
    name = lb["LoadBalancerName"]
    lb_type = lb.get("Type", "")
    price_kind = PRICE_KIND_BY_TYPE.get(lb_type)
    state = lb.get("State", {}).get("Code", "")
    return make_finding(
        region,
        "elbv2",
        name,
        is_lab(name, tags, ctx.settings),
        arn=lb["LoadBalancerArn"],
        tags=tags,
        detail=f"{lb_type} {lb.get('Scheme', '')} {state}",
        age_days=age_days(lb.get("CreatedTime"), ctx.now),
        estimated_monthly_usd=monthly_usd(price_kind) if price_kind else None,
    )
