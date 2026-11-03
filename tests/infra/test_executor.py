import aws_cdk as cdk
from aws_cdk import assertions
from aws_cdk.assertions import Match

from lab_janitor.markers import (
    KEEP_TAG_KEY,
    KEEP_TAG_VALUE,
    LAB_NAME_PREFIX,
    LAB_TAG_KEY,
    LAB_TAG_VALUE,
    lambda_env,
)
from lab_janitor.stack import LabJanitorStack
from tests.infra.conftest import ENV
from tests.infra.helpers import (
    EXECUTOR_HANDLER,
    actions,
    executor_statements,
    render,
    resources,
)

PARTITION = "<AWS::Partition>"
ACCOUNT = "123456789012"
EC2_DELETE_ACTIONS = ["ec2:DeleteSnapshot", "ec2:DeleteVolume", "ec2:ReleaseAddress"]
NAMED_ACTIONS = [
    "cloudwatch:DeleteDashboards",
    "internetmonitor:DeleteMonitor",
    "internetmonitor:GetMonitor",
    "internetmonitor:UpdateMonitor",
]
MUTATING_ACTIONS = [
    "cloudwatch:DeleteDashboards",
    "ec2:DeleteSnapshot",
    "ec2:DeleteVolume",
    "ec2:ReleaseAddress",
    "internetmonitor:DeleteMonitor",
    "internetmonitor:UpdateMonitor",
]
EC2_RESOURCES = [
    f"arn:{PARTITION}:ec2:*::snapshot/*",
    f"arn:{PARTITION}:ec2:*:{ACCOUNT}:volume/*",
    f"arn:{PARTITION}:ec2:*:{ACCOUNT}:elastic-ip/*",
]
DASHBOARD_ARN = f"arn:{PARTITION}:cloudwatch::{ACCOUNT}:dashboard/{LAB_NAME_PREFIX}*"
MONITOR_ARN = f"arn:{PARTITION}:internetmonitor:*:{ACCOUNT}:monitor/{LAB_NAME_PREFIX}*"
LAB_TAG = {f"aws:ResourceTag/{LAB_TAG_KEY}": LAB_TAG_VALUE}
DESCRIBE_ACTIONS = [
    "ec2:DescribeAddresses",
    "ec2:DescribeImages",
    "ec2:DescribeSnapshots",
    "ec2:DescribeVolumes",
]


def _function(template: assertions.Template) -> dict:
    functions = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Handler": EXECUTOR_HANDLER}}
    )
    (function,) = functions.values()
    return function


def _by_sid(template: assertions.Template, sid: str) -> dict:
    matches = [st for st in executor_statements(template) if st.get("Sid") == sid]
    assert len(matches) == 1, sid
    return matches[0]


def test_executor_function_settings(template: assertions.Template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Runtime": "python3.13",
            "Architectures": ["arm64"],
            "Handler": EXECUTOR_HANDLER,
            "MemorySize": 512,
            "Timeout": 600,
            "ReservedConcurrentExecutions": 1,
            "LoggingConfig": Match.object_like({"LogFormat": "JSON"}),
        },
    )
    log_group_id = _function(template)["Properties"]["LoggingConfig"]["LogGroup"]["Ref"]
    group = template.find_resources("AWS::Logs::LogGroup")[log_group_id]
    assert group["Properties"]["RetentionInDays"] == 90
    assert group["DeletionPolicy"] == "Delete"


def test_executor_reserved_concurrency_can_be_disabled(no_reserve_template: assertions.Template):
    assert "ReservedConcurrentExecutions" not in _function(no_reserve_template)["Properties"]


def test_executor_env_deletions_enabled_follows_dry_run(
    template: assertions.Template, live_template: assertions.Template
):
    variables = _function(template)["Properties"]["Environment"]["Variables"]
    assert variables == {
        **lambda_env(),
        "PLANS_BUCKET": {"Ref": "AuditPlansBucketDEC41664"},
        "AUDIT_TABLE": {"Ref": "AuditTable18A3095C"},
        "MIN_AGE_DAYS": "7",
        "DELETIONS_ENABLED": "false",
        "EVENT_BUS_NAME": "default",
    }
    live = _function(live_template)["Properties"]["Environment"]["Variables"]
    assert live["DELETIONS_ENABLED"] == "true"


def test_executor_delete_statements_are_double_locked(template: assertions.Template):
    ec2 = _by_sid(template, "ExecutorDeleteLabEc2")
    assert ec2["Effect"] == "Allow"
    assert sorted(actions(ec2)) == EC2_DELETE_ACTIONS
    assert sorted(render(r) for r in resources(ec2)) == sorted(EC2_RESOURCES)
    assert ec2["Condition"] == {"StringEquals": LAB_TAG}

    named = _by_sid(template, "ExecutorDeleteLabNamed")
    assert named["Effect"] == "Allow"
    assert sorted(actions(named)) == NAMED_ACTIONS
    assert sorted(render(r) for r in resources(named)) == sorted([DASHBOARD_ARN, MONITOR_ARN])
    assert "Condition" not in named


def test_executor_region_condition_only_when_configured():
    app = cdk.App(
        context={"reportEmail": "test@example.com", "allowedRegions": "us-east-1,eu-west-1"}
    )
    template = assertions.Template.from_stack(LabJanitorStack(app, "LabJanitorStack", env=ENV))
    regions = {"aws:RequestedRegion": ["us-east-1", "eu-west-1"]}
    ec2 = _by_sid(template, "ExecutorDeleteLabEc2")
    assert ec2["Condition"] == {"StringEquals": {**LAB_TAG, **regions}}
    named = _by_sid(template, "ExecutorDeleteLabNamed")
    assert named["Condition"] == {"StringEquals": regions}


def test_executor_deny_kept(template: assertions.Template):
    deny = _by_sid(template, "ExecutorDenyKept")
    assert deny["Effect"] == "Deny"
    assert sorted(actions(deny)) == MUTATING_ACTIONS
    assert deny["Resource"] == "*"
    assert deny["Condition"] == {
        "StringEquals": {f"aws:ResourceTag/{KEEP_TAG_KEY}": KEEP_TAG_VALUE}
    }


def test_executor_reverify_reads(template: assertions.Template):
    reverify = _by_sid(template, "ExecutorReverify")
    assert reverify["Effect"] == "Allow"
    assert sorted(actions(reverify)) == DESCRIBE_ACTIONS
    assert reverify["Resource"] == "*"

    tags = [
        st
        for st in executor_statements(template)
        if "cloudwatch:ListTagsForResource" in actions(st)
    ]
    assert len(tags) == 1
    (tag_read,) = tags
    assert actions(tag_read) == ["cloudwatch:ListTagsForResource"]
    assert [render(r) for r in resources(tag_read)] == [DASHBOARD_ARN]


def test_executor_reads_plans_and_approvals_writes_run_events(template: assertions.Template):
    statements = executor_statements(template)

    (plan_read,) = [st for st in statements if "s3:GetObjectVersion" in actions(st)]
    assert sorted(actions(plan_read)) == ["s3:GetObject", "s3:GetObjectVersion"]
    assert [render(r) for r in resources(plan_read)] == ["<AuditPlansBucketDEC41664.Arn>/plans/*"]

    table = [{"Fn::GetAtt": ["AuditTable18A3095C", "Arn"]}]
    (put,) = [st for st in statements if "dynamodb:PutItem" in actions(st)]
    assert actions(put) == ["dynamodb:PutItem"]
    assert resources(put) == table
    assert put["Condition"] == {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["RUN#*"]}}
    (get,) = [st for st in statements if "dynamodb:GetItem" in actions(st)]
    assert actions(get) == ["dynamodb:GetItem"]
    assert resources(get) == table
    assert get["Condition"] == {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["APPROVAL#*"]}}

    (events,) = [st for st in statements if "events:PutEvents" in actions(st)]
    assert actions(events) == ["events:PutEvents"]
    assert [render(r) for r in resources(events)] == [
        f"arn:{PARTITION}:events:us-east-1:{ACCOUNT}:event-bus/default"
    ]


def test_executor_is_not_a_plan_writer(template: assertions.Template):
    granted = {a for st in executor_statements(template) for a in actions(st)}
    assert "s3:PutObject" not in granted
    assert "dynamodb:UpdateItem" not in granted


def test_executor_cannot_list_or_tag(template: assertions.Template):
    granted = {a for st in executor_statements(template) for a in actions(st)}
    describes = {a for a in granted if ":Describe" in a}
    assert describes == set(DESCRIBE_ACTIONS)
    for a in granted:
        operation = a.split(":")[1]
        assert not operation.startswith(("List", "Tag", "Untag", "CreateTags")) or (
            a == "cloudwatch:ListTagsForResource"
        ), a
        assert not a.startswith(("sns:", "states:", "iam:")), a
