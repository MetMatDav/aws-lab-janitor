from aws_cdk import assertions
from aws_cdk.assertions import Match

INSECURE_TRANSPORT = {"Bool": {"aws:SecureTransport": "false"}}


def _ssl_deny_statements(policies: dict, document_key: str) -> list[dict]:
    return [
        statement
        for policy in policies.values()
        for statement in policy["Properties"][document_key]["Statement"]
        if statement["Effect"] == "Deny" and statement.get("Condition") == INSECURE_TRANSPORT
    ]


def test_topics_enforce_ssl_and_subscriptions_have_dlq(template: assertions.Template):
    topics = template.find_resources("AWS::SNS::Topic")
    assert sorted(t["Properties"]["DisplayName"] for t in topics.values()) == [
        "Lab Janitor Alerts",
        "Lab Janitor Reports",
    ]

    topic_policies = template.find_resources("AWS::SNS::TopicPolicy")
    protected = {
        topic["Ref"]
        for policy in topic_policies.values()
        if _ssl_deny_statements({"p": policy}, "PolicyDocument")
        for topic in policy["Properties"]["Topics"]
    }
    assert protected == set(topics)

    (dlq_id,) = template.find_resources("AWS::SQS::Queue")
    subscriptions = template.find_resources(
        "AWS::SNS::Subscription",
        Match.object_like({"Properties": {"Protocol": "email"}}),
    )
    assert len(subscriptions) == 2
    for subscription in subscriptions.values():
        assert subscription["Properties"]["RedrivePolicy"] == {
            "deadLetterTargetArn": {"Fn::GetAtt": [dlq_id, "Arn"]}
        }


def test_dlq_is_encrypted_and_ssl_only(template: assertions.Template):
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {"SqsManagedSseEnabled": True, "MessageRetentionPeriod": 14 * 24 * 3600},
    )

    queue_policies = template.find_resources("AWS::SQS::QueuePolicy")
    assert _ssl_deny_statements(queue_policies, "PolicyDocument")
