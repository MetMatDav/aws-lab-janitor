"""Refuse to publish to a topic nobody is subscribed to.

SNS accepts a publish to a topic whose only subscription is still pending
confirmation, or was removed through the unsubscribe link of an email, and
reports neither a delivery nor a failure. The notifier therefore checks the
subscriptions before it writes or sends anything: a run that cannot reach its
reader fails in that state and the failure goes to the alerts topic.
"""

from botocore.client import BaseClient

# A pending or deleted subscription is listed with a word instead of an ARN.
CONFIRMED_PREFIX = "arn:"


class NoConfirmedSubscriberError(Exception):
    """The topic has no confirmed subscription, so a publish would reach nobody."""


def require_subscriber(sns: BaseClient, topic_arn: str) -> None:
    """Raise ``NoConfirmedSubscriberError`` unless ``topic_arn`` has a confirmed subscription."""
    pages = sns.get_paginator("list_subscriptions_by_topic").paginate(TopicArn=topic_arn)
    for page in pages:
        for subscription in page["Subscriptions"]:
            if subscription["SubscriptionArn"].startswith(CONFIRMED_PREFIX):
                return
    raise NoConfirmedSubscriberError(
        f"topic {topic_arn} has no confirmed subscription, so nothing was published: "
        "confirm the subscription and start a new run"
    )
