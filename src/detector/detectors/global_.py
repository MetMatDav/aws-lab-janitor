"""Global detectors: S3 buckets, CloudFront distributions and CloudWatch dashboards.

These services are not regional, so every finding and error uses the region ``"global"`` and
every client is created in us-east-1. Buckets and distributions are reported for visibility
only; a dashboard is a candidate only when its name carries the lab prefix and its tags were
read successfully without the keep tag (fail closed).
"""

from botocore.exceptions import ClientError

from detector.detectors.common import first_blocker, make_finding, read_tags
from detector.sweep import SweepContext, collect, safe
from shared.findings import ErrorFinding, Finding
from shared.marking import age_days, is_lab_tagged

REGION = "global"
CLIENT_REGION = "us-east-1"
CDK_BUCKET_PREFIX = "cdk-"
NO_TAG_SET = "NoSuchTagSet"
TAGS_UNKNOWN = "tags unknown"


def detect_global(ctx: SweepContext, findings: list[Finding], errors: list[ErrorFinding]) -> None:
    """Inspect the global services, one ``safe`` block per listing and per tag lookup."""

    def client(service: str):
        # Fetched inside each safe block so a client-creation failure is recorded, not raised.
        return ctx.clients.get(service, CLIENT_REGION)

    def bucket_tags(name: str) -> list[dict]:
        """Tags of one bucket; empty when the lookup fails (marking falls back to the prefix)."""

        def fetch() -> list[dict]:
            try:
                return client("s3").get_bucket_tagging(Bucket=name).get("TagSet", [])
            except ClientError as exc:
                # A bucket without tags answers NoSuchTagSet: that is no tags, not an error.
                if exc.response.get("Error", {}).get("Code") == NO_TAG_SET:
                    return []
                raise

        return read_tags(ctx, errors, REGION, "s3:GetBucketTagging", fetch) or []

    def dashboard_tags(arn: str) -> list[dict] | None:
        """Tags of one dashboard, or None when the lookup failed."""

        def fetch() -> list[dict]:
            return client("cloudwatch").list_tags_for_resource(ResourceARN=arn).get("Tags", [])

        return read_tags(ctx, errors, REGION, "cloudwatch:ListTagsForResource", fetch)

    def run_buckets() -> None:
        for bucket in collect(client("s3"), "list_buckets", "Buckets", deadline=ctx.deadline):
            if bucket["Name"].startswith(CDK_BUCKET_PREFIX):
                continue
            findings.append(_bucket(bucket, bucket_tags(bucket["Name"]), ctx))

    def run_distributions() -> None:
        for distribution in collect(
            client("cloudfront"),
            "list_distributions",
            "DistributionList.Items",
            deadline=ctx.deadline,
        ):
            findings.append(_distribution(distribution, ctx))

    def run_dashboards() -> None:
        for dashboard in collect(
            client("cloudwatch"), "list_dashboards", "DashboardEntries", deadline=ctx.deadline
        ):
            name = dashboard["DashboardName"]
            arn = dashboard.get("DashboardArn") or _dashboard_arn(name, ctx)
            lab = name.startswith(ctx.settings.name_prefix)
            tags = dashboard_tags(arn) if lab else []
            findings.append(_dashboard(dashboard, arn, lab, tags, ctx))

    safe(ctx, errors, REGION, "s3:ListBuckets", run_buckets)
    safe(ctx, errors, REGION, "cloudfront:ListDistributions", run_distributions)
    safe(ctx, errors, REGION, "cloudwatch:ListDashboards", run_dashboards)


def _bucket(bucket: dict, tags: list[dict], ctx: SweepContext) -> Finding:
    """S3 bucket; never a candidate. Names are lowercase, so the prefix check ignores case."""
    name = bucket["Name"]
    settings = ctx.settings
    lab = name.lower().startswith(settings.name_prefix.lower()) or is_lab_tagged(tags, settings)
    return make_finding(
        REGION,
        "s3-bucket",
        name,
        lab,
        arn=f"arn:aws:s3:::{name}",
        tags=tags,
        detail="bucket",
        age_days=age_days(bucket.get("CreationDate"), ctx.now),
    )


def _distribution(distribution: dict, ctx: SweepContext) -> Finding:
    """CloudFront distribution; never lab and never a candidate."""
    domain, enabled = distribution.get("DomainName", ""), distribution.get("Enabled")
    return make_finding(
        REGION,
        "cloudfront",
        distribution["Id"],
        False,
        arn=distribution.get("ARN"),
        detail=f"{domain} enabled={enabled}",
        # The list API has no creation time; the last modification is the closest age we get.
        age_days=age_days(distribution.get("LastModifiedTime"), ctx.now),
    )


def _dashboard_arn(name: str, ctx: SweepContext) -> str:
    """Dashboards are global: their ARN has no region."""
    return f"arn:aws:cloudwatch::{ctx.account}:dashboard/{name}"


def _dashboard(
    dashboard: dict, arn: str, lab: bool, tags: list[dict] | None, ctx: SweepContext
) -> Finding:
    """Dashboard verdict; ``tags`` is None when the tag lookup of a lab dashboard failed."""
    settings = ctx.settings
    candidate, reason = False, ""
    if lab:
        blocker = TAGS_UNKNOWN if tags is None else first_blocker(tags, settings)
        candidate = blocker is None
        reason = f"name prefix {settings.name_prefix}" if candidate else blocker or ""
    return make_finding(
        REGION,
        "cw-dashboard",
        dashboard["DashboardName"],
        lab,
        arn=arn,
        tags=tags or [],
        detail="dashboard",
        candidate=candidate,
        reason=reason,
        age_days=age_days(dashboard.get("LastModified"), ctx.now),
    )
