"""Finding types every detector produces."""

from dataclasses import dataclass, field

KINDS: frozenset[str] = frozenset(
    {
        "ec2-instance",
        "nat-gateway",
        "elastic-ip",
        "public-ipv4",
        "ebs-volume",
        "ebs-snapshot",
        "ami",
        "vpc-endpoint",
        "elbv2",
        "rds-instance",
        "rds-cluster",
        "elasticache",
        "internet-monitor",
        "s3-bucket",
        "cloudfront",
        "cw-dashboard",
    }
)


@dataclass
class Finding:
    """A resource found in the account, with the verdict on whether it can be cleaned up."""

    region: str
    kind: str
    resource_id: str
    arn: str | None
    detail: str
    lab: bool
    candidate: bool
    reason: str
    age_days: int | None
    estimated_monthly_usd: float | None
    # Passes every rule except the audit-history one; the next run's age check reads it.
    eligible: bool = False
    tags: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        """JSON-ready dict with camelCase keys; tags are omitted."""
        return {
            "region": self.region,
            "kind": self.kind,
            "resourceId": self.resource_id,
            "arn": self.arn,
            "detail": self.detail,
            "lab": self.lab,
            "candidate": self.candidate,
            "eligible": self.eligible,
            "reason": self.reason,
            "ageDays": self.age_days,
            "estimatedMonthlyUsd": self.estimated_monthly_usd,
        }


@dataclass
class ErrorFinding:
    """A service/region the detector could not inspect."""

    region: str
    service: str
    error: str
    classification: str

    def to_dict(self) -> dict:
        """JSON-ready dict."""
        return {
            "region": self.region,
            "service": self.service,
            "error": self.error,
            "classification": self.classification,
        }
