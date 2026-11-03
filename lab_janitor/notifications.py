"""SNS topics for the weekly report and for operational alerts."""

from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from lab_janitor.config import JanitorConfig


class Notifications(Construct):
    """Report and alert topics with email subscriptions.

    Both topics deny non-TLS publishes. Each email subscription redrives
    undeliverable messages to the shared dead-letter queue so a delivery
    failure is never silent.
    """

    def __init__(
        self, scope: Construct, construct_id: str, *, config: JanitorConfig, dlq: sqs.IQueue
    ) -> None:
        super().__init__(scope, construct_id)

        self.reports_topic = sns.Topic(
            self, "ReportsTopic", display_name="Lab Janitor Reports", enforce_ssl=True
        )
        self.alerts_topic = sns.Topic(
            self, "AlertsTopic", display_name="Lab Janitor Alerts", enforce_ssl=True
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
            "AlertsEmail",
            topic=self.alerts_topic,
            protocol=sns.SubscriptionProtocol.EMAIL,
            endpoint=config.alert_email,
            dead_letter_queue=dlq,
        )
