"""The janitor stack."""

from typing import Any

from aws_cdk import Duration, Stack, Tags
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from lab_janitor.audit import AuditStore
from lab_janitor.config import JanitorConfig
from lab_janitor.detector import Detector
from lab_janitor.notifications import Notifications

DESCRIPTION = "Lab janitor: weekly sweep of lab leftovers with plan, approval and audit trail"


class LabJanitorStack(Stack):
    """Weekly sweeper for lab leftovers.

    Composes the shared dead-letter queue, the notification topics, the audit
    store and the detector that writes the weekly plan. The approval workflow
    is added on top.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs: Any) -> None:
        super().__init__(scope, construct_id, description=DESCRIPTION, **kwargs)

        self.config = JanitorConfig.from_node(self.node)

        self.dlq = sqs.Queue(
            self,
            "Dlq",
            retention_period=Duration.days(14),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
        )
        self.notifications = Notifications(self, "Notifications", config=self.config, dlq=self.dlq)
        self.audit = AuditStore(self, "Audit")

        # One asset for every janitor function: each handler lives in its own
        # package under src/ and they all share src/shared.
        code = lambda_.Code.from_asset("src", exclude=["__pycache__", "*.pyc", "**/__pycache__/**"])
        self.detector = Detector(self, "Detector", config=self.config, audit=self.audit, code=code)

        Tags.of(self).add("Project", "aws-lab-janitor")
        Tags.of(self).add("ManagedBy", "cdk")
