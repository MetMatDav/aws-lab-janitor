"""The notifier function: the only janitor component that sends email."""

from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from lab_janitor.audit import AuditStore
from lab_janitor.config import JanitorConfig
from lab_janitor.nag import acknowledge_janitor_function
from lab_janitor.notifications import Notifications

HANDLER = "notifier.handler.handler"


class Notifier(Construct):
    """Lambda function that sends the weekly report and the approval requests.

    The role can read plans (and their versions) under ``plans/``, publish to
    the reports and approvals topics, and create or refresh approval records
    under ``APPROVAL#`` partitions; nothing else. Unlike the detector it has
    no reserved concurrency: approval records are created conditionally, so
    concurrent invocations cannot overwrite each other.
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
                "APPROVALS_TOPIC_ARN": notifications.approvals_topic.topic_arn,
                "AUDIT_TABLE": audit.table.table_name,
                "APPROVAL_AUTH": config.approval_auth,
            },
            description="Lab janitor notifier: publishes the weekly report and approval requests",
        )

        audit.grant_plan_reads(self.function)
        audit.grant_approval_writes(self.function)
        notifications.reports_topic.grant_publish(self.function)
        notifications.approvals_topic.grant_publish(self.function)

        acknowledge_janitor_function(self.function, audit.plans_bucket)
