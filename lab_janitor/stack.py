"""The janitor stack."""

from typing import Any

from aws_cdk import Duration, Stack, Tags
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from lab_janitor.audit import AuditStore
from lab_janitor.config import JanitorConfig
from lab_janitor.detector import Detector
from lab_janitor.nag import acknowledge
from lab_janitor.notifications import Notifications
from lab_janitor.notifier import Notifier
from lab_janitor.workflow import Workflow

DESCRIPTION = "Lab janitor: weekly sweep of lab leftovers with plan, approval and audit trail"
STACK_TAGS = {"Project": "aws-lab-janitor", "ManagedBy": "cdk"}


class LabJanitorStack(Stack):
    """Weekly sweeper for lab leftovers.

    Composes the shared dead-letter queue, the notification topics, the audit
    store, the detector that writes the weekly plan, the notifier that emails
    the report, and the scheduled workflow that ties them together.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs: Any) -> None:
        # With @aws-cdk/core:explicitStackTags, Tags.of(self) reaches the resources but
        # not the stack itself, so the stack tags are passed here as well.
        tags = {**STACK_TAGS, **(kwargs.pop("tags", None) or {})}
        super().__init__(scope, construct_id, description=DESCRIPTION, tags=tags, **kwargs)

        self.config = JanitorConfig.from_node(self.node)

        self.dlq = sqs.Queue(
            self,
            "Dlq",
            retention_period=Duration.days(14),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
        )
        dlq_reason = (
            "This queue is the dead-letter queue of the scheduler; a second-level DLQ adds nothing"
        )
        acknowledge(
            self.dlq,
            {
                "AwsSolutions::AwsSolutions-SQS3": dlq_reason,
                "Serverless::Serverless-SQSRedrivePolicy": dlq_reason,
            },
        )
        self.notifications = Notifications(self, "Notifications", config=self.config, dlq=self.dlq)
        self.audit = AuditStore(self, "Audit")

        # One asset for every janitor function: each handler lives in its own
        # package under src/ and they all share src/shared.
        code = lambda_.Code.from_asset("src", exclude=["__pycache__", "*.pyc", "**/__pycache__/**"])
        self.detector = Detector(self, "Detector", config=self.config, audit=self.audit, code=code)
        self.notifier = Notifier(
            self,
            "Notifier",
            config=self.config,
            audit=self.audit,
            notifications=self.notifications,
            code=code,
        )
        notifier_role = self.notifier.function.role
        if notifier_role is None:  # Function always creates a role when none is passed in.
            raise RuntimeError("notifier function has no execution role")
        self.notifications.restrict_publishers(notifier_role.role_arn)
        self.workflow = Workflow(
            self,
            "Workflow",
            config=self.config,
            detector=self.detector,
            notifier=self.notifier,
            notifications=self.notifications,
            audit=self.audit,
            dlq=self.dlq,
        )

        for key, value in STACK_TAGS.items():
            Tags.of(self).add(key, value)
