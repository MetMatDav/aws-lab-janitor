import json

import aws_cdk as cdk
import pytest
from aws_cdk import assertions
from aws_cdk import aws_sqs as sqs
from aws_cdk.assertions import Match

from lab_janitor.config import JanitorConfig
from lab_janitor.notifications import Notifications
from tests.infra.conftest import ENV
from tests.infra.helpers import NOTIFIER_HANDLER, function_role_id

INSECURE_TRANSPORT = {"Bool": {"aws:SecureTransport": "false"}}


def _ssl_deny_statements(policies: dict, document_key: str) -> list[dict]:
    return [
        statement
        for policy in policies.values()
        for statement in policy["Properties"][document_key]["Statement"]
        if statement["Effect"] == "Deny" and statement.get("Condition") == INSECURE_TRANSPORT
    ]


def test_three_topics_and_three_subscriptions(template: assertions.Template):
    topics = template.find_resources("AWS::SNS::Topic")
    assert sorted(t["Properties"]["DisplayName"] for t in topics.values()) == [
        "Lab Janitor Alerts",
        "Lab Janitor Approvals",
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
    assert len(subscriptions) == 3
    approvals_id = _topic_id(template, "Lab Janitor Approvals")
    redriven = [
        sub["Properties"]
        for sub in subscriptions.values()
        if sub["Properties"]["TopicArn"]["Ref"] != approvals_id
    ]
    assert len(redriven) == 2
    for properties in redriven:
        assert properties["RedrivePolicy"] == {
            "deadLetterTargetArn": {"Fn::GetAtt": [dlq_id, "Arn"]}
        }

    endpoints = {
        sub["Properties"]["TopicArn"]["Ref"]: sub["Properties"]["Endpoint"]
        for sub in subscriptions.values()
    }
    assert endpoints[approvals_id] == "approver@example.com"
    assert endpoints[_topic_id(template, "Lab Janitor Reports")] == "test@example.com"


def test_approval_emails_are_never_parked_in_a_queue(template: assertions.Template):
    """An approval email carries a link that decides a plan.

    The approvals topic is closed to new subscribers for that reason. A
    redrive policy would hand the same message body to a queue whose readers
    nothing here restricts, so an approval email that SNS cannot deliver is
    dropped instead (ADR-0015).
    """
    approvals_id = _topic_id(template, "Lab Janitor Approvals")
    subscriptions = template.find_resources(
        "AWS::SNS::Subscription",
        {"Properties": {"TopicArn": {"Ref": approvals_id}}},
    )
    assert len(subscriptions) == 1
    (subscription,) = subscriptions.values()
    assert "RedrivePolicy" not in subscription["Properties"]

    # And the queue no longer accepts messages from that topic at all.
    queue_policies = template.find_resources("AWS::SQS::QueuePolicy")
    senders = {
        json.dumps(statement.get("Condition", {}))
        for policy in queue_policies.values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        if statement["Effect"] == "Allow"
    }
    assert senders
    assert not any(approvals_id in sender for sender in senders)


def test_dlq_is_encrypted_and_ssl_only(template: assertions.Template):
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {"SqsManagedSseEnabled": True, "MessageRetentionPeriod": 14 * 24 * 3600},
    )

    queue_policies = template.find_resources("AWS::SQS::QueuePolicy")
    assert _ssl_deny_statements(queue_policies, "PolicyDocument")


def test_no_subscription_leaks_the_address_into_logical_ids(cli_template: dict):
    resources = cli_template["Resources"]
    paths = [
        resource.get("Metadata", {}).get("aws:cdk:path", "") for resource in resources.values()
    ]
    assert any(paths), "path metadata must be enabled for this check to mean anything"
    endpoints = {
        resource["Properties"]["Endpoint"]
        for resource in resources.values()
        if resource["Type"] == "AWS::SNS::Subscription"
    }
    # The fixture uses a distinct address per topic, so each one is checked.
    assert endpoints == {"cli-test@example.org", "cli-approver@example.net"}
    for logical_id in resources:
        assert "@" not in logical_id, logical_id
        for marker in ("exampleorg", "examplenet", "clitest", "cliapprover"):
            assert marker not in logical_id.lower(), logical_id
    for path in paths:
        assert "@" not in path, path
        assert "example" not in path, path


def _topic_id(template: assertions.Template, display_name: str) -> str:
    topics = template.find_resources(
        "AWS::SNS::Topic", {"Properties": {"DisplayName": display_name}}
    )
    (topic_id,) = topics
    return topic_id


def _topic_policy_statements(template: assertions.Template, display_name: str) -> list[dict]:
    topic_id = _topic_id(template, display_name)
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


@pytest.mark.parametrize("display_name", ["Lab Janitor Reports", "Lab Janitor Approvals"])
def test_reports_and_approvals_topics_have_no_cloudwatch_allow(
    template: assertions.Template, display_name: str
):
    statements = _topic_policy_statements(template, display_name)
    assert statements
    assert not _cloudwatch_allows(statements)


ROOT_ARN = {"Fn::Join": ["", ["arn:", {"Ref": "AWS::Partition"}, ":iam::123456789012:root"]]}
CFN_EXEC_ROLE_ARN = {
    "Fn::Sub": (
        "arn:${AWS::Partition}:iam::123456789012:role/"
        "cdk-hnb659fds-cfn-exec-role-123456789012-us-east-1"
    )
}


def _principal_denies(statements: list[dict]) -> list[dict]:
    """Deny statements for every principal other than the TLS-only deny."""
    return [
        st
        for st in statements
        if st["Effect"] == "Deny"
        and st.get("Principal") == "*"
        and st.get("Condition") != INSECURE_TRANSPORT
    ]


def test_approvals_topic_denies_subscribe_except_cfn_exec_role_and_root(
    template: assertions.Template,
):
    topic_id = _topic_id(template, "Lab Janitor Approvals")
    denies = [
        st
        for st in _principal_denies(_topic_policy_statements(template, "Lab Janitor Approvals"))
        if "sns:Subscribe" in st["Action"]
    ]
    assert len(denies) == 1
    (deny,) = denies
    assert sorted(deny["Action"]) == [
        "sns:AddPermission",
        "sns:RemovePermission",
        "sns:SetTopicAttributes",
        "sns:Subscribe",
    ]
    assert deny["Resource"] == {"Ref": topic_id}
    assert deny["Condition"] == {
        "StringNotEquals": {"aws:PrincipalArn": [CFN_EXEC_ROLE_ARN, ROOT_ARN]}
    }


def test_approvals_topic_denies_publish_except_notifier_role(template: assertions.Template):
    topic_id = _topic_id(template, "Lab Janitor Approvals")
    role_id = function_role_id(template, NOTIFIER_HANDLER)
    denies = [
        st
        for st in _principal_denies(_topic_policy_statements(template, "Lab Janitor Approvals"))
        if st["Action"] == "sns:Publish"
    ]
    assert len(denies) == 1
    (deny,) = denies
    assert deny["Resource"] == {"Ref": topic_id}
    assert deny["Condition"] == {
        "StringNotEquals": {"aws:PrincipalArn": {"Fn::GetAtt": [role_id, "Arn"]}}
    }


def test_other_topics_have_no_principal_denies(template: assertions.Template):
    # Only the approvals topic is locked down; the reports and alerts topics
    # keep just the TLS deny, which has a SecureTransport condition.
    for display_name in ("Lab Janitor Reports", "Lab Janitor Alerts"):
        statements = _topic_policy_statements(template, display_name)
        assert not _principal_denies(statements)


def _bare_notifications(synthesizer: cdk.IStackSynthesizer | None = None) -> Notifications:
    app = cdk.App(context={"reportEmail": "test@example.com"})
    stack = cdk.Stack(app, "Bare", env=ENV, synthesizer=synthesizer)
    config = JanitorConfig.from_node(stack.node)
    return Notifications(stack, "Notifications", config=config, dlq=sqs.Queue(stack, "Dlq"))


def test_approvals_topic_needs_the_default_synthesizer():
    with pytest.raises(RuntimeError, match="DefaultStackSynthesizer"):
        _bare_notifications(cdk.LegacyStackSynthesizer())


def test_restrict_publishers_runs_once():
    notifications = _bare_notifications()
    role_arn = "arn:aws:iam::123456789012:role/Notifier"
    notifications.restrict_publishers(role_arn)
    with pytest.raises(RuntimeError, match="restrict_publishers"):
        notifications.restrict_publishers(role_arn)
