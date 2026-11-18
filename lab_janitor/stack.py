"""The janitor stack."""

from typing import Any

from aws_cdk import CfnOutput, Duration, Stack, Tags
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from lab_janitor.alarms import Alarms
from lab_janitor.approval_api import ApprovalApi
from lab_janitor.audit import AuditStore
from lab_janitor.config import JanitorConfig
from lab_janitor.detector import Detector
from lab_janitor.executor import Executor
from lab_janitor.nag import acknowledge
from lab_janitor.notifications import Notifications
from lab_janitor.notifier import Notifier
from lab_janitor.trail import AuditTrail
from lab_janitor.workflow import Workflow

DESCRIPTION = "Lab janitor: weekly sweep of lab leftovers with plan, approval and audit trail"
STACK_TAGS = {"Project": "aws-lab-janitor", "ManagedBy": "cdk"}


class LabJanitorStack(Stack):
    """Weekly sweeper for lab leftovers.

    Composes the shared dead-letter queue, the notification topics, the audit
    store and the trail that records writes to it, the detector that writes
    the weekly plan, the notifier that emails
    the report and the approval request, the approval endpoint, the executor
    that applies an approved plan, the scheduled workflow that ties them
    together, and the alarms that watch all of it.
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
            "This queue is the dead-letter queue of the scheduler and of the report and alert "
            "emails; a dead-letter queue of its own adds nothing"
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
        # createTrail=none leaves CloudTrail to whoever owns the account.
        self.trail = (
            None
            if self.config.create_trail == "none"
            else AuditTrail(self, "Trail", mode=self.config.create_trail, audit=self.audit)
        )

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
        self.approval_api = ApprovalApi(
            self, "ApprovalApi", config=self.config, audit=self.audit, code=code
        )
        self.notifier.function.add_environment("APPROVAL_URL", self.approval_api.url)
        self.executor = Executor(self, "Executor", config=self.config, audit=self.audit, code=code)
        self.workflow = Workflow(
            self,
            "Workflow",
            config=self.config,
            detector=self.detector,
            notifier=self.notifier,
            executor=self.executor,
            notifications=self.notifications,
            audit=self.audit,
            dlq=self.dlq,
        )
        self.alarms = Alarms(
            self,
            "Alarms",
            config=self.config,
            state_machine=self.workflow.state_machine,
            reminder_machine=self.workflow.reminder_machine,
            notifications=self.notifications,
            dlq=self.dlq,
        )

        # Stack-level outputs keep their logical ids stable and readable.
        # None of them carries an email address.
        CfnOutput(self, "StateMachineArn", value=self.workflow.state_machine.state_machine_arn)
        CfnOutput(self, "PlansBucketName", value=self.audit.plans_bucket.bucket_name)
        CfnOutput(self, "AuditTableName", value=self.audit.table.table_name)
        CfnOutput(self, "ApprovalApiUrl", value=self.approval_api.url)
        if self.trail is not None:
            CfnOutput(self, "TrailLogBucketName", value=self.trail.log_bucket.bucket_name)

        for key, value in STACK_TAGS.items():
            Tags.of(self).add(key, value)
