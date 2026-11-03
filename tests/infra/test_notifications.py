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


def test_no_logical_id_or_path_contains_email(cli_template: dict):
    resources = cli_template["Resources"]
    paths = [
        resource.get("Metadata", {}).get("aws:cdk:path", "") for resource in resources.values()
    ]
    assert any(paths), "path metadata must be enabled for this check to mean anything"
    for logical_id in resources:
        assert "@" not in logical_id, logical_id
        assert "exampleorg" not in logical_id.lower(), logical_id
    for path in paths:
        assert "@" not in path, path


def _topic_policy_statements(template: assertions.Template, display_name: str) -> list[dict]:
    topics = template.find_resources(
        "AWS::SNS::Topic", {"Properties": {"DisplayName": display_name}}
    )
    (topic_id,) = topics
    return [
        statement
        for policy in template.find_resources("AWS::SNS::TopicPolicy").values()
        if {"Ref": topic_id} in policy["Properties"]["Topics"]
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]


def _cloudwatch_allows(statements: list[dict]) -> list[dict]:
    return [
        st
        for st in statements
        if st["Effect"] == "Allow"
        and st.get("Principal") == {"Service": "cloudwatch.amazonaws.com"}
    ]


def test_alerts_topic_lets_cloudwatch_alarms_in_this_account_publish(
    template: assertions.Template,
):
    # enforce_ssl replaces the default topic policy, so without this Allow the
    # alarms could not publish to the alerts topic.
    topics = template.find_resources(
        "AWS::SNS::Topic", {"Properties": {"DisplayName": "Lab Janitor Alerts"}}
    )
    (alerts_id,) = topics
    allows = _cloudwatch_allows(_topic_policy_statements(template, "Lab Janitor Alerts"))
    assert len(allows) == 1
    (allow,) = allows
    assert allow["Action"] == "sns:Publish"
    assert allow["Resource"] == {"Ref": alerts_id}
    assert allow["Condition"] == {
        "StringEquals": {"aws:SourceAccount": "123456789012"},
        "ArnLike": {
            "aws:SourceArn": {
                "Fn::Join": [
                    "",
                    [
                        "arn:",
                        {"Ref": "AWS::Partition"},
                        ":cloudwatch:us-east-1:123456789012:alarm:*",
                    ],
                ]
            }
        },
    }


def test_reports_topic_has_no_cloudwatch_allow(template: assertions.Template):
    statements = _topic_policy_statements(template, "Lab Janitor Reports")
    assert statements
    assert not _cloudwatch_allows(statements)
