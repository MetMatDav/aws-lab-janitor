from aws_cdk import assertions
from aws_cdk.assertions import Match

from tests.infra.helpers import NOTIFIER_HANDLER, actions, resources, role_statements


def _notifier(template: assertions.Template) -> dict:
    functions = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Handler": NOTIFIER_HANDLER}}
    )
    (function,) = functions.values()
    return function


def _reports_topic_id(template: assertions.Template) -> str:
    topics = template.find_resources(
        "AWS::SNS::Topic", {"Properties": {"DisplayName": "Lab Janitor Reports"}}
    )
    (topic_id,) = topics
    return topic_id


def test_notifier_function_settings(template: assertions.Template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Runtime": "python3.13",
            "Architectures": ["arm64"],
            "Handler": NOTIFIER_HANDLER,
            "MemorySize": 256,
            "Timeout": 60,
            "LoggingConfig": Match.object_like({"LogFormat": "JSON"}),
        },
    )
    assert "ReservedConcurrentExecutions" not in _notifier(template)["Properties"]


def test_notifier_environment(template: assertions.Template):
    variables = _notifier(template)["Properties"]["Environment"]["Variables"]
    assert variables == {
        "PLANS_BUCKET": {"Ref": "AuditPlansBucketDEC41664"},
        "REPORTS_TOPIC_ARN": {"Ref": _reports_topic_id(template)},
    }


def test_notifier_log_group_retention_90_days(template: assertions.Template):
    log_group_id = _notifier(template)["Properties"]["LoggingConfig"]["LogGroup"]["Ref"]
    group = template.find_resources("AWS::Logs::LogGroup")[log_group_id]
    assert group["Properties"]["RetentionInDays"] == 90
    assert group["DeletionPolicy"] == "Delete"


def test_notifier_reads_plans_and_publishes_reports_only(template: assertions.Template):
    statements = role_statements(template, NOTIFIER_HANDLER)
    reads = [st for st in statements if "s3:GetObject" in actions(st)]
    assert len(reads) == 1
    (read,) = reads
    assert sorted(actions(read)) == ["s3:GetObject", "s3:GetObjectVersion"]
    (resource,) = resources(read)
    join = resource["Fn::Join"][1]
    assert join[0] == {"Fn::GetAtt": ["AuditPlansBucketDEC41664", "Arn"]}
    assert join[1] == "/plans/*"

    publishes = [st for st in statements if "sns:Publish" in actions(st)]
    assert len(publishes) == 1
    assert resources(publishes[0]) == [{"Ref": _reports_topic_id(template)}]

    granted = {a for st in statements for a in actions(st)}
    assert granted == {"s3:GetObject", "s3:GetObjectVersion", "sns:Publish"}
