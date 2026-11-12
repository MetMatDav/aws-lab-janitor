"""Surface 7: IAM and state-machine guardrails, synthesized in process (no AWS call).

One stack in the widest configuration (``approvalAuth=both``, a wildcard approver role
ARN that ``lab_janitor.config`` accepts, live deletions) is searched for grants and data
flows the per-construct tests do not look at.
"""

import json

import aws_cdk as cdk
import pytest
from aws_cdk import assertions

from lab_janitor.stack import LabJanitorStack
from tests.infra.helpers import (
    actions,
    detector_statements,
    reminder_definition,
    render,
    resources,
    state_machine_definition,
)

WILDCARD_APPROVER = "arn:aws:iam::123456789012:role/*"
TASK_TOKEN_PATH = "$$.Task.Token"  # noqa: S105 - a JSONPath, not a secret
APPROVAL_ID_PATH = "$.approvalRequest.approvalId"


@pytest.fixture(scope="module")
def template() -> assertions.Template:
    app = cdk.App(
        context={
            "reportEmail": "owner@example.com",
            "approvalEmail": "approver@example.com",
            "dryRun": "false",
            "approvalAuth": "both",
            "approverRoleArn": WILDCARD_APPROVER,
            "allowedRegions": "us-east-1,eu-west-1",
        }
    )
    stack = LabJanitorStack(
        app, "LabJanitorStack", env=cdk.Environment(account="123456789012", region="us-east-1")
    )
    return assertions.Template.from_stack(stack)


def _states_mentioning(definition: dict, needle: str) -> set[str]:
    return {name for name, state in definition["States"].items() if needle in json.dumps(state)}


def test_task_token_leaves_the_state_machine_only_towards_the_notifier(template):
    """The token is handed to one task, ``RequestApproval``; no alert, log or audit state
    and no other machine ever receives it."""
    weekly = state_machine_definition(template)

    assert _states_mentioning(weekly, TASK_TOKEN_PATH) == {"RequestApproval"}
    assert TASK_TOKEN_PATH not in json.dumps(reminder_definition(template))
    payload = weekly["States"]["RequestApproval"]["Parameters"]["Payload"]
    assert [key for key, value in payload.items() if value == TASK_TOKEN_PATH] == ["taskToken.$"]


def test_approval_id_never_flows_into_an_email_sent_by_the_state_machine(template):
    """The id is a bearer credential while pending: only the notifier may email it.

    It may reach the notifier (request, reminder), the executor and the DynamoDB keys of
    the record and its mirror; never an ``sns:publish`` state such as ``NotifyFailure``.
    """
    weekly = state_machine_definition(template)
    holders = _states_mentioning(weekly, APPROVAL_ID_PATH)
    publishers = {
        name
        for name, state in weekly["States"].items()
        if "sns:publish" in str(state.get("Resource", ""))
    }

    assert publishers == {"NotifyFailure"}
    assert holders == {
        "StartReminder",
        "RequestApproval",
        "Apply",
        "RecordRejected",
        "MirrorRejected",
        "RecordExpired",
        "MirrorExpired",
        "RecordApplied",
    }
    assert not holders & publishers
    assert "$.approvalRequest" not in json.dumps(weekly["States"]["NotifyFailure"])
    assert "States.JsonToString($)" not in json.dumps(weekly)


def test_execution_input_is_read_once_and_only_into_config(template):
    """Foothold (c): whatever keys an execution is started with land under ``$.config``.

    ``detect``, ``approval`` and ``approvalRequest`` are written by the machine itself, so
    an input carrying them cannot pre-seed a plan digest, a decision or an approval id.
    """
    weekly = state_machine_definition(template)

    assert _states_mentioning(weekly, "$$.Execution.Input") == {"Normalize"}
    normalize = weekly["States"]["Normalize"]
    assert list(normalize["Parameters"]) == ["config.$"]
    assert "ResultPath" not in normalize
    for name, state in weekly["States"].items():
        if state["Type"] == "Task" and name != "Detect":
            assert state.get("ResultPath", "$") != "$.detect", name
    assert weekly["States"]["Detect"]["ResultPath"] == "$.detect"
    assert weekly["States"]["NewApprovalId"]["ResultPath"] == "$.approvalRequest"
    assert weekly["States"]["RequestApproval"]["ResultPath"] == "$.approval"
    assert weekly["States"]["Apply"]["Parameters"] == {
        "detect.$": "$.detect",
        "config.$": "$.config",
        "approval.$": "$.approval",
        "approvalId.$": APPROVAL_ID_PATH,
    }


def test_wildcard_approver_arn_is_never_used_as_an_iam_principal_or_resource(template):
    """``-c approverRoleArn=arn:...:role/*`` passes validation. It must stay an inert
    string in the handler's environment (where it matches nobody), not become a grant."""
    document = template.to_json()
    functions = template.find_resources("AWS::Lambda::Function")
    carriers = [
        logical_id
        for logical_id, resource in document["Resources"].items()
        if WILDCARD_APPROVER in json.dumps(resource)
    ]

    assert len(carriers) == 1
    (carrier,) = carriers
    assert carrier in functions
    assert functions[carrier]["Properties"]["Handler"] == "approval.handler.handler"
    assert (
        functions[carrier]["Properties"]["Environment"]["Variables"]["APPROVER_ROLE_ARN"]
        == WILDCARD_APPROVER
    )


def test_region_lock_covers_every_delete_grant_when_regions_are_configured(template):
    """Each Allow that deletes or disables must carry the ``aws:RequestedRegion`` lock."""
    functions = template.find_resources("AWS::Lambda::Function")
    assert functions
    checked = 0
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            mutating = [
                action
                for action in actions(statement)
                if action.split(":")[1].startswith(("Delete", "Release", "Update"))
                and action.split(":")[0] in {"ec2", "cloudwatch", "internetmonitor"}
            ]
            if statement["Effect"] != "Allow" or not mutating:
                continue
            assert statement["Condition"]["StringEquals"]["aws:RequestedRegion"] == [
                "us-east-1",
                "eu-west-1",
            ], statement
            checked += 1
    assert checked == 2


@pytest.mark.xfail(
    strict=True,
    reason="the detector's dynamodb:Query grant covers the base table, not only the "
    "by-resource index it uses: the sweep role can read an APPROVAL# record, task token "
    "included, by querying its partition key",
)
def test_detector_can_query_only_the_index_it_uses(template):
    """Foothold: code running as the detector role that knows a pending approval id.

    The detector only ever queries ``IndexName=by-resource``. Approval records have no
    ``resourceId``, so they are absent from that index; they are reachable through the
    table ARN the same statement grants. The role cannot send the task callback, so this
    is a confidentiality gap of the token, not an approval bypass.
    """
    queries = [
        statement
        for statement in detector_statements(template)
        if statement["Effect"] == "Allow" and "dynamodb:Query" in actions(statement)
    ]

    assert queries
    for statement in queries:
        for resource in resources(statement):
            assert render(resource).endswith("/index/by-resource"), render(resource)
