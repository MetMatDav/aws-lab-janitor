"""Database detectors: RDS instances, RDS/Aurora clusters and ElastiCache clusters.

These resources are reported for visibility only; none of them is ever a candidate.
"""

from detector.detectors.common import make_finding
from detector.sweep import SweepContext, collect, safe
from shared.findings import ErrorFinding, Finding
from shared.marking import age_days, is_lab

HEADLESS_CLUSTER = "no instances: storage and backups still billed"


def detect_rds(
    region: str, ctx: SweepContext, findings: list[Finding], errors: list[ErrorFinding]
) -> None:
    """Inspect RDS instances and clusters in ``region``, one ``safe`` block per call."""

    def rds():
        # Fetched inside each safe block so a client-creation failure is recorded, not raised.
        return ctx.clients.get("rds", region)

    def run_instances() -> None:
        for db in collect(rds(), "describe_db_instances", "DBInstances", deadline=ctx.deadline):
            findings.append(_db_instance(db, region, ctx))

    def run_clusters() -> None:
        for cluster in collect(rds(), "describe_db_clusters", "DBClusters", deadline=ctx.deadline):
            findings.append(_db_cluster(cluster, region, ctx))

    safe(ctx, errors, region, "rds:DescribeDBInstances", run_instances)
    safe(ctx, errors, region, "rds:DescribeDBClusters", run_clusters)


def detect_elasticache(
    region: str, ctx: SweepContext, findings: list[Finding], errors: list[ErrorFinding]
) -> None:
    """Inspect ElastiCache clusters in ``region``; each tag lookup has its own ``safe`` block."""

    def elasticache():
        return ctx.clients.get("elasticache", region)

    def cluster_tags(arn: str) -> list[dict]:
        """Tags of one cluster; empty when the lookup fails (marking falls back to the prefix)."""
        tags: list[dict] = []

        def fetch() -> None:
            tags.extend(
                collect(
                    elasticache(),
                    "list_tags_for_resource",
                    "TagList",
                    deadline=ctx.deadline,
                    ResourceName=arn,
                )
            )

        safe(ctx, errors, region, "elasticache:ListTagsForResource", fetch)
        return tags

    def run_clusters() -> None:
        for cluster in collect(
            elasticache(), "describe_cache_clusters", "CacheClusters", deadline=ctx.deadline
        ):
            # Without an ARN there is nothing to look the tags up by; the
            # cluster is still reported, marked by its name alone.
            arn = cluster.get("ARN")
            tags = cluster_tags(arn) if arn else []
            findings.append(_cache_cluster(cluster, tags, region, ctx))

    safe(ctx, errors, region, "elasticache:DescribeCacheClusters", run_clusters)


def _db_instance(db: dict, region: str, ctx: SweepContext) -> Finding:
    """RDS DB instance with class, engine and status; never a candidate."""
    identifier = db["DBInstanceIdentifier"]
    tags = db.get("TagList", [])
    db_class, engine = db.get("DBInstanceClass", ""), db.get("Engine", "")
    return make_finding(
        region,
        "rds-instance",
        identifier,
        is_lab(identifier, tags, ctx.settings),
        arn=db.get("DBInstanceArn"),
        tags=tags,
        detail=f"{db_class} {engine} {db.get('DBInstanceStatus', '')}",
        age_days=age_days(db.get("InstanceCreateTime"), ctx.now),
    )


def _db_cluster(cluster: dict, region: str, ctx: SweepContext) -> Finding:
    """RDS/Aurora cluster; a cluster without instances still bills storage and backups."""
    identifier = cluster["DBClusterIdentifier"]
    tags = cluster.get("TagList", [])
    members = cluster.get("DBClusterMembers", [])
    engine, status = cluster.get("Engine", ""), cluster.get("Status", "")
    if members:
        detail = f"{engine} {status} {len(members)} instances"
    else:
        detail = f"{HEADLESS_CLUSTER} ({engine} {status})"
    return make_finding(
        region,
        "rds-cluster",
        identifier,
        is_lab(identifier, tags, ctx.settings),
        arn=cluster.get("DBClusterArn"),
        tags=tags,
        detail=detail,
        age_days=age_days(cluster.get("ClusterCreateTime"), ctx.now),
    )


def _cache_cluster(cluster: dict, tags: list[dict], region: str, ctx: SweepContext) -> Finding:
    """ElastiCache cluster with node type, engine and status; never a candidate."""
    cluster_id = cluster["CacheClusterId"]
    node_type, engine = cluster.get("CacheNodeType", ""), cluster.get("Engine", "")
    return make_finding(
        region,
        "elasticache",
        cluster_id,
        is_lab(cluster_id, tags, ctx.settings),
        arn=cluster.get("ARN"),
        tags=tags,
        detail=f"{node_type} {engine} {cluster.get('CacheClusterStatus', '')}",
        age_days=age_days(cluster.get("CacheClusterCreateTime"), ctx.now),
    )
