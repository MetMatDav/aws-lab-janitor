"""The notifier function: the only janitor component that sends email."""

from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from lab_janitor.audit import PLANS_PREFIX, AuditStore
from lab_janitor.config import JanitorConfig
from lab_janitor.nag import acknowledge_janitor_function
from lab_janitor.notifications import Notifications

HANDLER = "notifier.handler.handler"


class Notifier(Construct):
    """Lambda function that turns a detector run into the weekly report email.

    The role can read plans (and their versions) under ``plans/`` and publish
    to the reports topic; nothing else. Unlike the detector it has no reserved
    concurrency: it only reads, so concurrent invocations cannot race.
    """

    def __init__(  # noqa: PLR0913 - construct props are keyword-only dependencies
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: JanitorConfig,
        audit: AuditStore,
        notifications: Notifications,
        code: lambda_.Code,
    ) -> None:
        super().__init__(scope, construct_id)
        # config is part of the construct contract (approval and result modes
        # in later plans read it); the report mode needs nothing from it.
        self.config = config

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
            memory_size=256,
            timeout=Duration.minutes(1),
            log_group=self.log_group,
            logging_format=lambda_.LoggingFormat.JSON,
            environment={
                "PLANS_BUCKET": audit.plans_bucket.bucket_name,
                "REPORTS_TOPIC_ARN": notifications.reports_topic.topic_arn,
            },
            description="Lab janitor notifier: publishes the weekly report",
        )

        # GetObjectVersion because the detector hands over the exact report
        # version it wrote, so the email always matches that run.
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:GetObjectVersion"],
                resources=[audit.plans_bucket.arn_for_objects(f"{PLANS_PREFIX}*")],
            )
        )
        notifications.reports_topic.grant_publish(self.function)

        acknowledge_janitor_function(self.function, audit.plans_bucket)
