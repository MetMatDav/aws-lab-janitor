"""SNS topics for the weekly report, the approval requests and operational alerts."""

from aws_cdk import ArnFormat, DefaultStackSynthesizer, Fn, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from lab_janitor.config import JanitorConfig


class Notifications(Construct):
    """Report, approval and alert topics with email subscriptions.

    Every topic denies non-TLS publishes. Each email subscription redrives
    undeliverable messages to the shared dead-letter queue so a delivery
    failure is never silent.

    The approvals topic carries links that approve a deletion, so it is
    locked down further: only CloudFormation and the account root may change
    who receives it, and only the notifier (see ``restrict_publishers``) may
    publish to it.
    """

    def __init__(
        self, scope: Construct, construct_id: str, *, config: JanitorConfig, dlq: sqs.IQueue
    ) -> None:
        super().__init__(scope, construct_id)

        self.reports_topic = sns.Topic(
            self, "ReportsTopic", display_name="Lab Janitor Reports", enforce_ssl=True
        )
        self.approvals_topic = sns.Topic(
            self, "ApprovalsTopic", display_name="Lab Janitor Approvals", enforce_ssl=True
        )
        self.alerts_topic = sns.Topic(
            self, "AlertsTopic", display_name="Lab Janitor Alerts", enforce_ssl=True
        )
        # enforce_ssl creates a topic policy, which replaces the default one, so
        # CloudWatch alarms (they publish as the cloudwatch.amazonaws.com service
        # principal) need an explicit Allow, limited to alarms in this account.
        stack = Stack.of(self)
        self.alerts_topic.add_to_resource_policy(
            iam.PolicyStatement(
                principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[self.alerts_topic.topic_arn],
                conditions={
                    "StringEquals": {"aws:SourceAccount": stack.account},
                    "ArnLike": {
                        "aws:SourceArn": stack.format_arn(
                            service="cloudwatch",
                            resource="alarm",
                            resource_name="*",
                            arn_format=ArnFormat.COLON_RESOURCE_NAME,
                        )
                    },
                },
            )
        )

        # Fixed construct ids: topic.add_subscription would name the
        # subscription after the address, leaking it into logical ids and
        # aws:cdk:path metadata.
        sns.Subscription(
            self,
            "ReportsEmail",
            topic=self.reports_topic,
            protocol=sns.SubscriptionProtocol.EMAIL,
            endpoint=config.report_email,
            dead_letter_queue=dlq,
        )
        sns.Subscription(
            self,
            "ApprovalsEmail",
            topic=self.approvals_topic,
            protocol=sns.SubscriptionProtocol.EMAIL,
            endpoint=config.approval_email,
            dead_letter_queue=dlq,
        )
        sns.Subscription(
            self,
            "AlertsEmail",
            topic=self.alerts_topic,
            protocol=sns.SubscriptionProtocol.EMAIL,
            endpoint=config.alert_email,
            dead_letter_queue=dlq,
        )

        self._deny_subscription_changes(stack)
        self._publishers_restricted = False

    def _deny_subscription_changes(self, stack: Stack) -> None:
        """Let only CloudFormation and the account root change the approvals audience.

        A new subscriber would receive working approval links, so adding one
        (or rewriting the topic policy to allow it) must go through a stack
        deployment, which runs as the CloudFormation execution role.
        """
        synthesizer = stack.synthesizer
        exec_role_arn = (
            synthesizer.cloud_formation_execution_role_arn
            if isinstance(synthesizer, DefaultStackSynthesizer)
            else None
        )
        if exec_role_arn is None:
            raise RuntimeError("approvals topic needs the DefaultStackSynthesizer")
        self.approvals_topic.add_to_resource_policy(
            iam.PolicyStatement(
                effect=iam.Effect.DENY,
                principals=[iam.StarPrincipal()],
                actions=[
                    "sns:Subscribe",
                    "sns:SetTopicAttributes",
                    "sns:AddPermission",
                    "sns:RemovePermission",
                ],
                resources=[self.approvals_topic.topic_arn],
                conditions={
                    "StringNotEquals": {
                        "aws:PrincipalArn": [
                            # The ARN carries ${AWS::Partition} placeholders.
                            Fn.sub(exec_role_arn),
                            f"arn:{stack.partition}:iam::{stack.account}:root",
                        ]
                    }
                },
            )
        )

    def restrict_publishers(self, role_arn: str) -> None:
        """Make ``role_arn`` the only principal that can publish approval requests.

        Called once by the stack after the notifier exists. Like
        ``AuditStore.allow_plan_writes`` this is a Deny for everyone else; the
        publisher still needs an identity policy granting ``sns:Publish``.
        """
        if self._publishers_restricted:
            raise RuntimeError("restrict_publishers was already called")
        self._publishers_restricted = True
        self.approvals_topic.add_to_resource_policy(
            iam.PolicyStatement(
                effect=iam.Effect.DENY,
                principals=[iam.StarPrincipal()],
                actions=["sns:Publish"],
                resources=[self.approvals_topic.topic_arn],
                conditions={"StringNotEquals": {"aws:PrincipalArn": role_arn}},
            )
        )
