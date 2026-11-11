"""The IAM simulation script: policy extraction, the case matrix and the report."""

import json
import re
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber

from tests.iam import simulate_policies as sim

EXECUTOR = "executor.handler.handler"
DELETE_ACTIONS = {
    "cloudwatch:DeleteDashboards",
    "internetmonitor:DeleteMonitor",
    "ec2:DeleteSnapshot",
    "ec2:DeleteVolume",
    "ec2:ReleaseAddress",
}


def test_role_policy_resolves_every_intrinsic(cli_template: dict):
    policy = sim.role_policy(cli_template, EXECUTOR)
    text = json.dumps(policy)

    assert policy["Version"] == "2012-10-17"
    assert "Fn::" not in text
    assert '"Ref"' not in text
    assert "arn:aws:cloudwatch::123456789012:dashboard/LAB-*" in text
    assert sim.TABLE_ARN in text
    assert f"{sim.PLANS_BUCKET_ARN}/plans/*" in text
    sids = {statement.get("Sid") for statement in policy["Statement"]}
    assert {"ExecutorDeleteLabEc2", "ExecutorDeleteLabNamed", "ExecutorDenyKept"} <= sids


def test_role_policy_holds_only_that_role(cli_template: dict):
    actions = {
        action
        for statement in sim.role_policy(cli_template, EXECUTOR)["Statement"]
        for action in (
            [statement["Action"]] if isinstance(statement["Action"], str) else statement["Action"]
        )
    }

    # The detector's sweep and the notifier's publish belong to other roles.
    assert "ec2:DescribeInstances" not in actions
    assert "sns:Publish" not in actions
    assert "ec2:DeleteSnapshot" in actions


def test_cases_cover_the_spec_matrix():
    pairs = {(case.action, case.expected) for case in sim.CASES}

    for action in DELETE_ACTIONS:
        assert (action, "allowed") in pairs, action
        assert (action, "implicitDeny") in pairs, action
    for action in DELETE_ACTIONS - {"ec2:DeleteVolume"}:
        assert (action, "explicitDeny") in pairs, action
    for action in ("ec2:TerminateInstances", "s3:DeleteBucket", "ec2:CreateTags"):
        assert (action, "implicitDeny") in pairs, action
    names = [case.name for case in sim.CASES]
    assert len(names) == len(set(names))
    assert {case.expected for case in sim.CASES} == {"allowed", "implicitDeny", "explicitDeny"}


def test_evaluate_sends_tags_and_leading_keys_as_context():
    iam = boto3.client("iam", region_name="us-east-1")
    policy = {"Version": "2012-10-17", "Statement": []}
    case = sim.Case(
        name="example",
        action="ec2:DeleteSnapshot",
        resource="arn:aws:ec2:us-east-1::snapshot/snap-0123456789abcdef0",
        expected="allowed",
        tags={"Purpose": "lab"},
        leading_keys=("RUN#1",),
    )
    with Stubber(iam) as stubber:
        stubber.add_response(
            "simulate_custom_policy",
            {
                "EvaluationResults": [
                    {"EvalActionName": case.action, "EvalDecision": "explicitDeny"}
                ]
            },
            {
                "PolicyInputList": [json.dumps(policy)],
                "ActionNames": [case.action],
                "ResourceArns": [case.resource],
                "ContextEntries": [
                    {
                        "ContextKeyName": "aws:ResourceTag/Purpose",
                        "ContextKeyValues": ["lab"],
                        "ContextKeyType": "string",
                    },
                    {
                        "ContextKeyName": "dynamodb:LeadingKeys",
                        "ContextKeyValues": ["RUN#1"],
                        "ContextKeyType": "stringList",
                    },
                ],
            },
        )

        assert sim.evaluate(iam, policy, case) == "explicitDeny"
        stubber.assert_no_pending_responses()


def test_synthesized_template_never_carries_a_real_account_id():
    # The CDK CLI overwrites CDK_DEFAULT_ACCOUNT with the caller's account, so
    # a template from cdk.out would not match the cases (and would leak the id).
    policy = sim.role_policy(sim.synthesized_template(), EXECUTOR)

    assert set(re.findall(r"\b\d{12}\b", json.dumps(policy))) == {sim.ACCOUNT}


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch, cli_template: dict) -> None:
    """No synthesis and no AWS client: the template comes from the session fixture."""
    monkeypatch.setattr(sim, "synthesized_template", lambda: cli_template)
    monkeypatch.setattr(sim, "iam_client", lambda _profile: None)


def test_main_writes_one_line_per_case_and_passes_when_all_match(
    tmp_path: Path, offline: None, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(sim, "evaluate", lambda _iam, _policy, case: case.expected)
    out = tmp_path / "report.txt"

    code = sim.main(["--out", str(out)])

    lines = out.read_text().splitlines()
    assert code == 0
    assert len([line for line in lines if line.startswith("PASS")]) == len(sim.CASES)
    assert not [line for line in lines if line.startswith("FAIL")]
    assert lines[-1] == f"{len(sim.CASES)} cases, {len(sim.CASES)} passed, 0 failed"


def test_main_fails_on_a_mismatch(tmp_path: Path, offline: None, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sim, "evaluate", lambda _iam, _policy, _case: "allowed")
    out = tmp_path / "report.txt"

    code = sim.main(["--out", str(out)])

    text = out.read_text()
    expected_failures = len([case for case in sim.CASES if case.expected != "allowed"])
    assert code == 1
    assert text.count("\nFAIL") == expected_failures
    assert "expected=explicitDeny  actual=allowed" in text
    # The evidence file is committed, so it says its account id is a placeholder.
    assert "Account id 123456789012 and all resource ids are placeholders." in text


def test_main_refuses_a_policy_with_another_account_id(
    tmp_path: Path, cli_template: dict, monkeypatch: pytest.MonkeyPatch
):
    foreign = json.loads(json.dumps(cli_template).replace(sim.ACCOUNT, "111122223333"))
    monkeypatch.setattr(sim, "synthesized_template", lambda: foreign)
    monkeypatch.setattr(sim, "iam_client", lambda _profile: pytest.fail("no AWS call expected"))
    out = tmp_path / "report.txt"

    assert sim.main(["--out", str(out)]) == 2
    assert not out.exists()
