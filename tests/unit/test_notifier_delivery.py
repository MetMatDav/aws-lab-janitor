"""The notifier refuses to publish to a topic nobody is subscribed to."""

import pytest

from notifier import delivery
from tests.unit.conftest import PENDING_SUBSCRIPTION, expect_subscriptions

TOPIC_ARN = "arn:aws:sns:us-east-1:123456789012:reports"
CONFIRMED = f"{TOPIC_ARN}:11111111-2222-3333-4444-555555555555"


def test_a_confirmed_subscription_is_enough(sns_stub):
    client, stubber = sns_stub
    expect_subscriptions(stubber, TOPIC_ARN, PENDING_SUBSCRIPTION, CONFIRMED)

    delivery.require_subscriber(client, TOPIC_ARN)

    stubber.assert_no_pending_responses()


@pytest.mark.parametrize(
    "subscription_arns",
    [(PENDING_SUBSCRIPTION,), ("Deleted",), (PENDING_SUBSCRIPTION, "Deleted")],
)
def test_unconfirmed_subscriptions_do_not_count(sns_stub, subscription_arns):
    client, stubber = sns_stub
    expect_subscriptions(stubber, TOPIC_ARN, *subscription_arns)

    with pytest.raises(delivery.NoConfirmedSubscriberError) as error:
        delivery.require_subscriber(client, TOPIC_ARN)

    assert str(error.value) == (
        f"topic {TOPIC_ARN} has no confirmed subscription, so nothing was published: "
        "confirm the subscription and start a new run"
    )


def test_a_topic_without_subscriptions_is_refused(sns_stub):
    client, stubber = sns_stub
    stubber.add_response(
        "list_subscriptions_by_topic", {"Subscriptions": []}, {"TopicArn": TOPIC_ARN}
    )

    with pytest.raises(delivery.NoConfirmedSubscriberError):
        delivery.require_subscriber(client, TOPIC_ARN)


def test_a_confirmed_subscription_on_a_later_page_counts(sns_stub):
    client, stubber = sns_stub
    stubber.add_response(
        "list_subscriptions_by_topic",
        {
            "Subscriptions": [{"SubscriptionArn": PENDING_SUBSCRIPTION, "Protocol": "email"}],
            "NextToken": "page-2",
        },
        {"TopicArn": TOPIC_ARN},
    )
    stubber.add_response(
        "list_subscriptions_by_topic",
        {"Subscriptions": [{"SubscriptionArn": CONFIRMED, "Protocol": "email"}]},
        {"TopicArn": TOPIC_ARN, "NextToken": "page-2"},
    )

    delivery.require_subscriber(client, TOPIC_ARN)

    stubber.assert_no_pending_responses()
