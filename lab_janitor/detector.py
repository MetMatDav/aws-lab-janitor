"""The detector function: weekly read-only sweep that writes the plan."""

from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from lab_janitor.audit import PLANS_PREFIX, AuditStore
from lab_janitor.config import JanitorConfig
from lab_janitor.markers import lambda_env

HANDLER = "detector.handler.handler"

# Everything the sweep reads, and nothing else. Each action is a List or
# Describe call (plus sts:GetCallerIdentity to learn the account id), so the
# role can see lab resources but cannot change them.
READ_ONLY_SWEEP_ACTIONS = [
    "ec2:DescribeRegions",
    "ec2:DescribeInstances",
    "ec2:DescribeNatGateways",
    "ec2:DescribeAddresses",
    "ec2:DescribeVolumes",
    "ec2:DescribeSnapshots",
    "ec2:DescribeImages",
    "ec2:DescribeVpcEndpoints",
    "ec2:DescribeNetworkInterfaces",
    "elasticloadbalancing:DescribeLoadBalancers",
    "elasticloadbalancing:DescribeTags",
    "rds:DescribeDBInstances",
    "rds:DescribeDBClusters",
    "rds:ListTagsForResource",
    "elasticache:DescribeCacheClusters",
    "elasticache:ListTagsForResource",
    "internetmonitor:ListMonitors",
    "internetmonitor:ListTagsForResource",
    "cloudwatch:ListDashboards",
    "cloudwatch:ListTagsForResource",
    "s3:ListAllMyBuckets",
    "s3:GetBucketTagging",
    "cloudfront:ListDistributions",
    "sts:GetCallerIdentity",
]


class Detector(Construct):
    """Lambda function that sweeps every region and writes the weekly plan.

    The role reads the account through an enumerated list of read-only
    actions, writes plans only under ``plans/`` in the plans bucket, and
    appends audit events only under ``RUN#`` partitions. The plans bucket
    denies plan writes from every other principal.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: JanitorConfig,
        audit: AuditStore,
        code: lambda_.Code,
    ) -> None:
        super().__init__(scope, construct_id)

        # Explicit group so retention and removal are under our control instead
        # of a group Lambda creates on first invocation and never expires.
        self.log_group = logs.LogGroup(
            self,
            "Logs",
            retention=logs.RetentionDays.THREE_MONTHS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.function = lambda_.Function(
            self,
            "Function",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler=HANDLER,
            code=code,
            memory_size=512,
            timeout=Duration.minutes(5),
            # One sweep at a time: two concurrent runs would race on the plan.
            reserved_concurrent_executions=1,
            log_group=self.log_group,
            logging_format=lambda_.LoggingFormat.JSON,
            environment={
                **lambda_env(),
                "PLANS_BUCKET": audit.plans_bucket.bucket_name,
                "AUDIT_TABLE": audit.table.table_name,
                "MIN_AGE_DAYS": str(config.min_age_days),
            },
            description="Lab janitor detector: sweeps all regions and writes the weekly plan",
        )

        # Resource "*" is required, not a shortcut: Describe*/List* calls such
        # as ec2:DescribeInstances, s3:ListAllMyBuckets and sts:GetCallerIdentity
        # do not support resource-level permissions, and the sweep must find
        # resources it does not know about yet, in every region. The action list
        # is what keeps this statement read-only.
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                sid="DetectorReadOnlySweep",
                actions=READ_ONLY_SWEEP_ACTIONS,
                resources=["*"],
            )
        )
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject"],
                resources=[audit.plans_bucket.arn_for_objects(f"{PLANS_PREFIX}*")],
            )
        )
        audit.grant_run_event_writes(self.function)
        audit.grant_resource_queries(self.function)

        role = self.function.role
        if role is None:  # Function always creates a role when none is passed in.
            raise RuntimeError("detector function has no execution role")
        audit.allow_plan_writes(role.role_arn)
