"""The executor function: applies the approved plan, one re-verified item at a time."""

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from lab_janitor.audit import AuditStore
from lab_janitor.config import JanitorConfig
from lab_janitor.markers import (
    KEEP_TAG_KEY,
    KEEP_TAG_VALUE,
    LAB_NAME_PREFIX,
    LAB_TAG_KEY,
    LAB_TAG_VALUE,
    lambda_env,
)
from lab_janitor.nag import (
    EXECUTOR_DESCRIBE_REASON,
    acknowledge,
    acknowledge_janitor_function,
    wildcard_arn_finding,
)

HANDLER = "executor.handler.handler"

EC2_DELETE_ACTIONS = ["ec2:DeleteSnapshot", "ec2:DeleteVolume", "ec2:ReleaseAddress"]
DASHBOARD_DELETE_ACTIONS = ["cloudwatch:DeleteDashboards"]
MONITOR_ACTIONS = [
    "internetmonitor:GetMonitor",
    "internetmonitor:UpdateMonitor",
    "internetmonitor:DeleteMonitor",
]
# Every action that changes a lab resource. ExecutorDenyKept denies all of
# them on anything carrying the keep tag, whatever the Allow statements say.
MUTATING_ACTIONS = [
    *EC2_DELETE_ACTIONS,
    *DASHBOARD_DELETE_ACTIONS,
    "internetmonitor:UpdateMonitor",
    "internetmonitor:DeleteMonitor",
]
REVERIFY_DESCRIBE_ACTIONS = [
    "ec2:DescribeSnapshots",
    "ec2:DescribeVolumes",
    "ec2:DescribeAddresses",
    "ec2:DescribeImages",
]

LAB_TAG_LOCK_REASON = (
    "EC2 ids are unknown at deploy time; the delete is limited by the lab-tag condition "
    "and the keep-tag Deny"
)
LAB_NAME_LOCK_REASON = (
    "Resource names are unknown at deploy time; the grant is limited to the LAB- name prefix "
    "and the keep-tag Deny"
)


class Executor(Construct):
    """Lambda function that deletes the approved, re-verified items of a plan.

    The delete grants are double-locked: EC2 deletes need the lab tag on the
    resource, dashboard and monitor deletes need the ``LAB-`` name prefix in
    the ARN, and an explicit Deny blocks every mutation on resources tagged
    to keep. With ``allowedRegions`` set, deletes are also limited to those
    regions. The role reads only the approved plan version and its approval
    record, appends ``RUN#`` audit events, and publishes run events on the
    default bus; it cannot list resources, tag them, or write plans.
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
            timeout=Duration.minutes(10),
            # One apply at a time when the account quota allows it; disable
            # with -c reservedConcurrency=false like the detector.
            reserved_concurrent_executions=1 if config.reserved_concurrency else None,
            log_group=self.log_group,
            logging_format=lambda_.LoggingFormat.JSON,
            environment={
                **lambda_env(),
                "PLANS_BUCKET": audit.plans_bucket.bucket_name,
                "AUDIT_TABLE": audit.table.table_name,
                "MIN_AGE_DAYS": str(config.min_age_days),
                # Deploy-time kill switch: a dry-run deployment cannot delete,
                # whatever the run input says.
                "DELETIONS_ENABLED": "false" if config.dry_run else "true",
                "EVENT_BUS_NAME": "default",
            },
            description="Lab janitor executor: deletes the approved, re-verified plan items",
        )

        stack = Stack.of(self)
        partition, account, region = stack.partition, stack.account, stack.region
        dashboard_arn = f"arn:{partition}:cloudwatch::{account}:dashboard/{LAB_NAME_PREFIX}*"
        monitor_arn = f"arn:{partition}:internetmonitor:*:{account}:monitor/{LAB_NAME_PREFIX}*"
        region_lock = (
            {"aws:RequestedRegion": list(config.allowed_regions)} if config.allowed_regions else {}
        )
        lab_tag = {f"aws:ResourceTag/{LAB_TAG_KEY}": LAB_TAG_VALUE}

        ec2_arns = [
            f"arn:{partition}:ec2:*::snapshot/*",
            f"arn:{partition}:ec2:*:{account}:volume/*",
            f"arn:{partition}:ec2:*:{account}:elastic-ip/*",
        ]

        self.function.add_to_role_policy(
            iam.PolicyStatement(
                sid="ExecutorDeleteLabEc2",
                actions=EC2_DELETE_ACTIONS,
                resources=ec2_arns,
                conditions={"StringEquals": {**lab_tag, **region_lock}},
            )
        )
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                sid="ExecutorDeleteLabNamed",
                actions=DASHBOARD_DELETE_ACTIONS + MONITOR_ACTIONS,
                resources=[dashboard_arn, monitor_arn],
                conditions={"StringEquals": region_lock} if region_lock else None,
            )
        )
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                sid="ExecutorDenyKept",
                effect=iam.Effect.DENY,
                actions=MUTATING_ACTIONS,
                resources=["*"],
                conditions={"StringEquals": {f"aws:ResourceTag/{KEEP_TAG_KEY}": KEEP_TAG_VALUE}},
            )
        )
        # Resource "*" because these Describe calls do not support
        # resource-level permissions; the action list keeps it read-only.
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                sid="ExecutorReverify",
                actions=REVERIFY_DESCRIBE_ACTIONS,
                resources=["*"],
            )
        )
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                sid="ExecutorReverifyDashboardTags",
                actions=["cloudwatch:ListTagsForResource"],
                resources=[dashboard_arn],
            )
        )
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["events:PutEvents"],
                resources=[f"arn:{partition}:events:{region}:{account}:event-bus/default"],
            )
        )
        audit.grant_plan_reads(self.function)
        audit.grant_run_event_writes(self.function)
        audit.grant_approval_reads(self.function)

        acknowledge_janitor_function(self.function, audit.plans_bucket)
        role = self.function.role
        if role is None:  # Function always creates a role when none is passed in.
            raise RuntimeError("executor function has no execution role")
        acknowledge(
            role,
            {
                "AwsSolutions::AwsSolutions-IAM5[Resource::*]": EXECUTOR_DESCRIBE_REASON,
                **{wildcard_arn_finding(self, arn): LAB_TAG_LOCK_REASON for arn in ec2_arns},
                wildcard_arn_finding(self, dashboard_arn): LAB_NAME_LOCK_REASON,
                wildcard_arn_finding(self, monitor_arn): LAB_NAME_LOCK_REASON,
            },
        )
