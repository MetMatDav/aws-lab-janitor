"""The janitor stack."""

from typing import Any

from aws_cdk import Duration, Stack, Tags
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from lab_janitor.audit import AuditStore
from lab_janitor.config import JanitorConfig
from lab_janitor.notifications import Notifications

DESCRIPTION = "Lab janitor: weekly sweep of lab leftovers with plan, approval and audit trail"


class LabJanitorStack(Stack):
    """Weekly sweeper for lab leftovers.

    Composes the shared dead-letter queue, the notification topics and the
    audit store. The detector and the approval workflow are added on top.
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

        Tags.of(self).add("Project", "aws-lab-janitor")
        Tags.of(self).add("ManagedBy", "cdk")
