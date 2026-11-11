"""CloudTrail for the audit trail itself: who wrote plans and audit items.

The plans bucket and the audit table are the janitor's evidence. Writes to
them are data events, which CloudTrail does not record unless a trail asks
for them. This construct creates that trail and the bucket it logs to.
"""

from aws_cdk import ArnFormat, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudtrail as cloudtrail
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct

from lab_janitor.audit import PLANS_PREFIX, AuditStore
from lab_janitor.nag import acknowledge

FULL = "full"
LOG_EXPIRATION_DAYS = 400
NONCURRENT_LOG_EXPIRATION_DAYS = 30
CLOUDTRAIL = "cloudtrail.amazonaws.com"


Selector = cloudtrail.CfnTrail.AdvancedEventSelectorProperty
Field = cloudtrail.CfnTrail.AdvancedFieldSelectorProperty


def _equals(field: str, value: str) -> Field:
    return Field(field=field, equal_to=[value])


class AuditTrail(Construct):
    """A trail for the data events of the plans prefix and the audit table.

    ``mode`` is the ``createTrail`` context value. ``full`` is for an account
    with no trail: multi-region, with management events as well.
    ``dataEventsOnly`` is for an account whose management events are already
    delivered by another trail (an organization trail, for example): it logs
    only the two kinds of data events, so nothing is recorded, or billed,
    twice. The stack does not create this construct for ``none``.

    The log bucket accepts writes from this trail only (``aws:SourceArn``),
    keeps 400 days of logs and is retained when the stack is deleted.
    """

    def __init__(
        self, scope: Construct, construct_id: str, *, mode: str, audit: AuditStore
    ) -> None:
        super().__init__(scope, construct_id)

        stack = Stack.of(self)
        # Named explicitly: the bucket policy needs the trail's ARN before the
        # trail exists, and the trail needs the policy before it can be created.
        trail_name = f"{stack.stack_name}-audit"
        trail_arn = stack.format_arn(
            service="cloudtrail",
            resource="trail",
            resource_name=trail_name,
            arn_format=ArnFormat.SLASH_RESOURCE_NAME,
        )

        self.log_bucket = s3.Bucket(
            self,
            "LogBucket",
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    expiration=Duration.days(LOG_EXPIRATION_DAYS),
                    noncurrent_version_expiration=Duration.days(NONCURRENT_LOG_EXPIRATION_DAYS),
                )
            ],
        )
        acknowledge(
            self.log_bucket,
            {
                "AwsSolutions::AwsSolutions-S1": (
                    "CloudTrail log destination; server access logs here would only log "
                    "the log deliveries"
                )
            },
        )
        from_this_trail = {"aws:SourceArn": trail_arn}
        principal = iam.ServicePrincipal(CLOUDTRAIL)
        self.log_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="CloudTrailAclCheck",
                principals=[principal],
                actions=["s3:GetBucketAcl"],
                resources=[self.log_bucket.bucket_arn],
                conditions={"StringEquals": from_this_trail},
            )
        )
        self.log_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="CloudTrailWrite",
                principals=[principal],
                actions=["s3:PutObject"],
                resources=[self.log_bucket.arn_for_objects(f"AWSLogs/{stack.account}/*")],
                conditions={
                    "StringEquals": {
                        **from_this_trail,
                        "s3:x-amz-acl": "bucket-owner-full-control",
                    }
                },
            )
        )

        full = mode == FULL
        management = [
            Selector(
                name="Management events",
                field_selectors=[_equals("eventCategory", "Management")],
            )
        ]
        data = [
            Selector(
                name="Plan objects",
                field_selectors=[
                    _equals("eventCategory", "Data"),
                    _equals("resources.type", "AWS::S3::Object"),
                    Field(
                        field="resources.ARN",
                        starts_with=[audit.plans_bucket.arn_for_objects(PLANS_PREFIX)],
                    ),
                ],
            ),
            Selector(
                name="Audit table items",
                field_selectors=[
                    _equals("eventCategory", "Data"),
                    _equals("resources.type", "AWS::DynamoDB::Table"),
                    _equals("resources.ARN", audit.table.table_arn),
                ],
            ),
        ]

        self.trail = cloudtrail.CfnTrail(
            self,
            "Trail",
            trail_name=trail_name,
            is_logging=True,
            s3_bucket_name=self.log_bucket.bucket_name,
            enable_log_file_validation=True,
            is_multi_region_trail=full,
            include_global_service_events=full,
            advanced_event_selectors=[*management, *data] if full else data,
        )
        policy = self.log_bucket.policy
        if policy is None:  # add_to_resource_policy always creates it.
            raise RuntimeError("trail log bucket has no bucket policy")
        self.trail.node.add_dependency(policy)
