from datetime import UTC, datetime

import pytest

from notifier import handler as notifier
from notifier import reminder
from shared.approvals import ApprovalRecord, ApprovalStore, expiry_for
from shared.dynamo import serialize
from shared.plan_summary import render_summary, summarize_plan

TABLE = "test-audit"
APPROVALS_ARN = "arn:aws:sns:us-east-1:123456789012:approvals"
APPROVAL_URL = "https://abc123.execute-api.us-east-1.amazonaws.com"
RUN_ID = "20261102T120000Z-abc1"
APPROVAL_ID = "11111111-aaaa-bbbb-cccc-222222222222"
CREATED = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 5, 21, 0, tzinfo=UTC)  # 60 hours later
MESSAGE_ID = "cccccccc-2222-3333-4444-555555555555"
SUBJECT = f"[Lab Janitor] Reminder: approval pending, run {RUN_ID}"

PLAN = {
    "runId": RUN_ID,
    "generatedAt": "2026-11-02T12:00:00+00:00",
    "candidates": [
        {
            "region": "global",
            "kind": "cw-dashboard",
            "resourceId": "LAB-demo",
            "arn": None,
            "detail": "dashboard",
            "lab": True,
            "candidate": True,
            "eligible": True,
            "reason": "name prefix LAB-",
            "ageDays": 3,
            "estimatedMonthlyUsd": 0.0,
        }
    ],
    "errors": [],
}
RECORD = ApprovalRecord(
    approval_id=APPROVAL_ID,
    run_id=RUN_ID,
    plan_version_id="plan-v1",
    plan_sha256="ab" * 32,
    plan_summary=summarize_plan(PLAN),
    status="PENDING",
    created_at=CREATED.isoformat(),
    expires_at=expiry_for(CREATED).isoformat(),
    approved_by=None,
    decided_at=None,
)


def record_item() -> dict:
    return serialize(
        {
            "pk": f"APPROVAL#{APPROVAL_ID}",
            "sk": "META",
            "approvalId": APPROVAL_ID,
            "runId": RUN_ID,
            "planVersionId": RECORD.plan_version_id,
            "planSha256": RECORD.plan_sha256,
            "planSummary": RECORD.plan_summary,
            "taskToken": "task-token-not-a-real-one",
            "status": "PENDING",
            "createdAt": RECORD.created_at,
            "expiresAt": RECORD.expires_at,
            "remindedAt": NOW.isoformat(),
        }
    )


def mark_params() -> dict:
    return {
        "TableName": TABLE,
        "Key": serialize({"pk": f"APPROVAL#{APPROVAL_ID}", "sk": "META"}),
        "UpdateExpression": "SET remindedAt = :now",
        "ConditionExpression": (
            "#s = :pending AND expiresAt > :now AND attribute_not_exists(remindedAt)"
        ),
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": serialize({":now": NOW.isoformat(), ":pending": "PENDING"}),
        "ReturnValues": "ALL_NEW",
    }


@pytest.fixture
def stubs(monkeypatch, sns_stub, ddb_stub):
    """Stubbed SNS and DynamoDB wired into ``build_dependencies``; the reminder reads no plan."""
    monkeypatch.setenv("APPROVALS_TOPIC_ARN", APPROVALS_ARN)
    monkeypatch.setenv("APPROVAL_URL", APPROVAL_URL)
    monkeypatch.setenv("APPROVAL_AUTH", "link")
    monkeypatch.setattr(
        notifier,
        "build_dependencies",
        lambda: (None, sns_stub[0], ApprovalStore(ddb_stub[0], TABLE)),
    )
    yield sns_stub[1], ddb_stub[1]
    for stubber in (sns_stub[1], ddb_stub[1]):
        stubber.assert_no_pending_responses()


def event() -> dict:
    return {"mode": "reminder", "approvalId": APPROVAL_ID}


def test_reminder_emails_the_approvals_topic_with_the_link(stubs):
    sns, ddb = stubs
    message = reminder.reminder_message(RECORD, APPROVAL_URL, "link", "abc1")
    ddb.add_response("update_item", {"Attributes": record_item()}, mark_params())
    sns.add_response(
        "publish",
        {"MessageId": MESSAGE_ID},
        {"TopicArn": APPROVALS_ARN, "Subject": SUBJECT, "Message": message},
    )

    result = reminder.send_reminder(event(), NOW)

    assert result == {"reminded": True, "messageId": MESSAGE_ID}
    assert message.splitlines()[0] == (
        f"Reminder: the deletion plan of run {RUN_ID} is still waiting for a decision."
    )
    assert f"Review and decide: {APPROVAL_URL}/approvals/{APPROVAL_ID}" in message
    assert "Type the confirmation code abc1 on that page." in message
    assert (
        f"This request expires at {RECORD.expires_at} (UTC). "
        "After that nothing is deleted for this run."
    ) in message
    assert f"Plan sha256: {RECORD.plan_sha256}" in message
    assert render_summary(RECORD.plan_summary) in message


def test_reminder_is_silent_when_the_approval_is_not_pending(stubs):
    _sns, ddb = stubs
    ddb.add_client_error(
        "update_item",
        service_error_code="ConditionalCheckFailedException",
        expected_params=mark_params(),
    )

    # No publish response is queued: any SNS call would fail the test.
    assert reminder.send_reminder(event(), NOW) == {"reminded": False}


def test_reminder_in_iam_mode_has_commands_and_no_link(stubs, monkeypatch):
    sns, ddb = stubs
    monkeypatch.setenv("APPROVAL_AUTH", "iam")
    message = reminder.reminder_message(RECORD, APPROVAL_URL, "iam", "abc1")
    ddb.add_response("update_item", {"Attributes": record_item()}, mark_params())
    sns.add_response(
        "publish",
        {"MessageId": MESSAGE_ID},
        {"TopicArn": APPROVALS_ARN, "Subject": SUBJECT, "Message": message},
    )

    assert reminder.send_reminder(event(), NOW)["reminded"] is True
    assert f"python scripts/approve.py --url {APPROVAL_URL} {APPROVAL_ID} approve" in message
    assert "/approvals/" not in message


def test_handler_dispatches_reminder_mode(monkeypatch):
    seen = []
    monkeypatch.setattr(reminder, "send_reminder", lambda event: seen.append(event) or {"ok": 1})

    assert notifier.handler(event(), None) == {"ok": 1}
    assert seen == [event()]
