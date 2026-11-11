import io
import logging
from datetime import UTC, datetime, timedelta

import pytest
from botocore.response import StreamingBody

from notifier import approval
from notifier import handler as notifier
from shared.approvals import APPROVAL_TTL_DAYS, ApprovalRecord, ApprovalStore, expiry_for
from shared.dynamo import serialize
from shared.plan import plan_bytes, plan_sha256
from shared.plan_store import PlanStore
from shared.plan_summary import render_summary, summarize_plan

BUCKET = "test-plans"
TABLE = "test-audit"
REPORTS_ARN = "arn:aws:sns:us-east-1:123456789012:reports"
APPROVALS_ARN = "arn:aws:sns:us-east-1:123456789012:approvals"
APPROVAL_URL = "https://abc123.execute-api.us-east-1.amazonaws.com"
RUN_ID = "20261102T120000Z-abc1"
APPROVAL_ID = "11111111-aaaa-bbbb-cccc-222222222222"
TASK_TOKEN = "task-token-not-a-real-one"  # noqa: S105 - test value
NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
APPROVALS_MESSAGE_ID = "aaaaaaaa-2222-3333-4444-555555555555"
REPORT_MESSAGE_ID = "bbbbbbbb-2222-3333-4444-555555555555"


def candidate(resource_id: str, region: str, cost: float | None) -> dict:
    return {
        "region": region,
        "kind": "nat-gateway",
        "resourceId": resource_id,
        "arn": None,
        "detail": "",
        "lab": True,
        "candidate": True,
        "eligible": True,
        "reason": "idle for 30 days",
        "ageDays": 30,
        "estimatedMonthlyUsd": cost,
    }


def make_plan(run_id: str = RUN_ID) -> dict:
    return {
        "runId": run_id,
        "generatedAt": "2026-11-02T12:00:00+00:00",
        "candidates": [
            candidate("nat-1", "us-east-1", 32.85),
            candidate("nat-2", "us-west-2", 1.5),
        ],
        "errors": [],
    }


def detect_result(run_id: str = RUN_ID, plan: dict | None = None, **overrides) -> dict:
    values = {
        "runId": run_id,
        "planKey": f"plans/{run_id}/plan.json",
        "planVersionId": "plan-v1",
        "planSha256": plan_sha256(plan_bytes(plan or make_plan(run_id))),
        "reportKey": f"plans/{run_id}/report.txt",
        "reportVersionId": "report-v1",
        "candidateCount": 2,
        "errorCount": 0,
        "findingCount": 2,
        "estimatedMonthlyUsd": 34.35,
        "dryRun": False,
    }
    values.update(overrides)
    return values


def approval_event(run_id: str = RUN_ID, **detect_overrides) -> dict:
    return {
        "mode": "approval",
        "taskToken": TASK_TOKEN,
        "approvalId": APPROVAL_ID,
        "detect": detect_result(run_id, **detect_overrides),
        "config": {"dryRun": False, "minAgeDays": 7},
    }


def body(data: bytes) -> dict:
    return {"Body": StreamingBody(io.BytesIO(data), len(data))}


def expected_record(run_id: str = RUN_ID) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=APPROVAL_ID,
        run_id=run_id,
        plan_version_id="plan-v1",
        plan_sha256=plan_sha256(plan_bytes(make_plan(run_id))),
        plan_summary=summarize_plan(make_plan(run_id)),
        status="PENDING",
        created_at=NOW.isoformat(),
        expires_at=expiry_for(NOW).isoformat(),
        approved_by=None,
        decided_at=None,
    )


def expected_item(record: ApprovalRecord) -> dict:
    return serialize(
        {
            "pk": f"APPROVAL#{record.approval_id}",
            "sk": "META",
            "approvalId": record.approval_id,
            "runId": record.run_id,
            "planVersionId": record.plan_version_id,
            "planSha256": record.plan_sha256,
            "planSummary": record.plan_summary,
            "taskToken": TASK_TOKEN,
            "status": "PENDING",
            "createdAt": record.created_at,
            "expiresAt": record.expires_at,
            "ttl": int((expiry_for(NOW) + timedelta(days=APPROVAL_TTL_DAYS)).timestamp()),
        }
    )


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("APPROVALS_TOPIC_ARN", APPROVALS_ARN)
    monkeypatch.setenv("APPROVAL_URL", APPROVAL_URL)
    monkeypatch.setenv("APPROVAL_AUTH", "link")


@pytest.fixture
def stubs(monkeypatch, env, s3_stub, sns_stub, ddb_stub):
    """Stubbed S3, SNS and DynamoDB wired into ``build_dependencies``."""
    monkeypatch.setattr(
        notifier,
        "build_dependencies",
        lambda: (
            PlanStore(s3_stub[0], BUCKET),
            sns_stub[0],
            ApprovalStore(ddb_stub[0], TABLE),
        ),
    )
    yield s3_stub[1], sns_stub[1], ddb_stub[1]
    for stubber in (s3_stub[1], sns_stub[1], ddb_stub[1]):
        stubber.assert_no_pending_responses()


def expect_plan_read(s3, run_id: str = RUN_ID, plan: dict | None = None) -> None:
    s3.add_response(
        "get_object",
        body(plan_bytes(plan or make_plan(run_id))),
        {"Bucket": BUCKET, "Key": f"plans/{run_id}/plan.json", "VersionId": "plan-v1"},
    )


def expect_record(ddb, run_id: str = RUN_ID) -> None:
    ddb.add_response(
        "put_item",
        {},
        {
            "TableName": TABLE,
            "Item": expected_item(expected_record(run_id)),
            "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)",
        },
    )


def expect_approvals_publish(sns, subject: str, message: str) -> None:
    sns.add_response(
        "publish",
        {"MessageId": APPROVALS_MESSAGE_ID},
        {"TopicArn": APPROVALS_ARN, "Subject": subject, "Message": message},
    )


def expect_report(s3, sns, run_id: str = RUN_ID) -> None:
    s3.add_response(
        "get_object",
        body(b"WEEKLY REPORT"),
        {"Bucket": BUCKET, "Key": f"plans/{run_id}/report.txt", "VersionId": "report-v1"},
    )
    sns.add_response(
        "publish",
        {"MessageId": REPORT_MESSAGE_ID},
        {
            "TopicArn": REPORTS_ARN,
            "Subject": "[Lab Janitor] Weekly report: 2 candidates, ~$34.35/month",
            "Message": "WEEKLY REPORT",
        },
    )


def link_block(confirmation: str = "abc1") -> str:
    return (
        f"Review and decide: {APPROVAL_URL}/approvals/{APPROVAL_ID}\n"
        f"Type the confirmation code {confirmation} on that page."
    )


def iam_block() -> str:
    command = f"python scripts/approve.py --url {APPROVAL_URL} {APPROVAL_ID}"
    return f"Approve with your AWS identity:\n{command} approve\n{command} reject"


def message_with(*auth_blocks: str, run_id: str = RUN_ID) -> str:
    plan = make_plan(run_id)
    sha = plan_sha256(plan_bytes(plan))
    return "\n\n".join(
        [
            "Lab Janitor wants to delete 2 resources (~$34.35/month).",
            *auth_blocks,
            f"This request expires at {expiry_for(NOW).isoformat()} (UTC).",
            f"Plan sha256: {sha}",
            render_summary(summarize_plan(plan)),
            f"Full plan: s3://{BUCKET}/plans/{run_id}/plan.json",
        ]
    )


SUBJECT = f"[Lab Janitor] Approval needed: 2 deletions, ~$34.35/month, run {RUN_ID}"


def run_request(stubs, message: str, subject: str = SUBJECT, run_id: str = RUN_ID) -> dict:
    s3, sns, ddb = stubs
    expect_plan_read(s3, run_id)
    expect_record(ddb, run_id)
    expect_approvals_publish(sns, subject, message)
    expect_report(s3, sns, run_id)
    return approval.request_approval(approval_event(run_id), now=NOW)


def test_request_approval_writes_record_and_publishes_links(stubs):
    result = run_request(stubs, message_with(link_block()))

    assert result == {
        "approvalId": APPROVAL_ID,
        "expiresAt": expiry_for(NOW).isoformat(),
        "messageId": APPROVALS_MESSAGE_ID,
    }


def test_link_message_has_links_and_no_cli_command(stubs, monkeypatch):
    captured: list[str] = []
    original = approval.approval_message

    def spy(*args):
        captured.append(original(*args))
        return captured[-1]

    monkeypatch.setattr(approval, "approval_message", spy)

    run_request(stubs, message_with(link_block()))

    (message,) = captured
    assert f"/approvals/{APPROVAL_ID}" in message
    assert "confirmation code abc1" in message
    assert "approve.py" not in message


def test_request_approval_iam_mode_sends_cli_command_not_links(stubs, monkeypatch):
    monkeypatch.setenv("APPROVAL_AUTH", "iam")
    message = message_with(iam_block())

    run_request(stubs, message)

    assert "Review and decide" not in message
    assert "confirmation code" not in message


def test_request_approval_both_mode_sends_both(stubs, monkeypatch):
    monkeypatch.setenv("APPROVAL_AUTH", "both")

    run_request(stubs, message_with(link_block(), iam_block()))


def test_request_approval_refuses_sha_mismatch(stubs):
    s3, _sns, _ddb = stubs
    expect_plan_read(s3)
    event = approval_event(planSha256="f" * 64)

    with pytest.raises(approval.PlanIntegrityError):
        approval.request_approval(event, now=NOW)
    # the fixture asserts no DynamoDB write and no publish were left pending or attempted


def test_request_approval_subject_falls_back_when_too_long(stubs):
    run_id = "20261102T120000Z-" + "x" * 60 + "-abc1"

    run_request(
        stubs,
        message_with(link_block(), run_id=run_id),
        subject="[Lab Janitor] Approval needed",
        run_id=run_id,
    )


def test_request_approval_also_publishes_the_report(stubs, sns_stub):
    topics: list[str] = []
    publish = sns_stub[0].publish

    def spy(**kwargs):
        topics.append(kwargs["TopicArn"])
        return publish(**kwargs)

    sns_stub[0].publish = spy

    run_request(stubs, message_with(link_block()))

    assert topics == [APPROVALS_ARN, REPORTS_ARN]


def test_unknown_auth_mode_raises():
    record = expected_record()

    with pytest.raises(ValueError, match="unknown approval auth mode"):
        approval.approval_message(record, record.plan_summary, APPROVAL_URL, "oauth", "abc1")


def test_request_approval_retry_refreshes_the_token_and_republishes(stubs, caplog):
    s3, sns, ddb = stubs
    expect_plan_read(s3)
    ddb.add_client_error(
        "put_item",
        service_error_code="ConditionalCheckFailedException",
        service_message="The conditional request failed",
        http_status_code=400,
        expected_params={
            "TableName": TABLE,
            "Item": expected_item(expected_record()),
            "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)",
        },
    )
    ddb.add_response(
        "update_item",
        {},
        {
            "TableName": TABLE,
            "Key": serialize({"pk": f"APPROVAL#{APPROVAL_ID}", "sk": "META"}),
            "UpdateExpression": "SET taskToken = :t",
            "ConditionExpression": "#s = :pending",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": serialize({":t": TASK_TOKEN, ":pending": "PENDING"}),
        },
    )
    expect_approvals_publish(sns, SUBJECT, message_with(link_block()))
    expect_report(s3, sns)

    with caplog.at_level(logging.INFO, logger="notifier"):
        result = approval.request_approval(approval_event(), now=NOW)

    assert result["approvalId"] == APPROVAL_ID
    assert "existing approval request refreshed" in caplog.text
    assert APPROVAL_ID not in caplog.text
    assert TASK_TOKEN not in caplog.text


def test_decision_sections_rejects_unknown_auth():
    with pytest.raises(ValueError, match="unknown approval auth mode: sms"):
        approval.decision_sections(APPROVAL_URL, APPROVAL_ID, "sms", "abc1")


def test_decision_sections_follow_the_auth_mode():
    link = approval.decision_sections(APPROVAL_URL, APPROVAL_ID, "link", "abc1")
    both = approval.decision_sections(APPROVAL_URL, APPROVAL_ID, "both", "abc1")

    assert ["\n".join(lines) for lines in link] == [link_block()]
    assert ["\n".join(lines) for lines in both] == [link_block(), iam_block()]


def test_subject_and_body_use_singular_for_one_candidate():
    plan = make_plan()
    plan["candidates"] = plan["candidates"][:1]
    summary = summarize_plan(plan)
    cost = f"~${summary['estimatedMonthlyUsd']:.2f}/month"

    message = approval.approval_message(expected_record(), summary, APPROVAL_URL, "link", "abc1")

    assert summary["candidateCount"] == 1
    assert approval.approval_subject(summary) == (
        f"[Lab Janitor] Approval needed: 1 deletion, {cost}, run {RUN_ID}"
    )
    assert message.splitlines()[0] == f"Lab Janitor wants to delete 1 resource ({cost})."


def test_subject_and_body_keep_the_plural_for_two_candidates():
    summary = summarize_plan(make_plan())

    message = approval.approval_message(expected_record(), summary, APPROVAL_URL, "link", "abc1")

    assert ": 2 deletions, " in approval.approval_subject(summary)
    assert message.startswith("Lab Janitor wants to delete 2 resources (")
