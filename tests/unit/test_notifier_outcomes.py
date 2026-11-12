import json

import pytest

from notifier import delivery
from notifier import handler as notifier
from tests.unit.conftest import PENDING_SUBSCRIPTION, expect_subscriptions

BUCKET = "test-plans"
TOPIC_ARN = "arn:aws:sns:us-east-1:123456789012:reports"
RUN_ID = "20261102T120000Z-abc1"
MESSAGE_ID = "11111111-2222-3333-4444-555555555555"
POINTER = f"Full plan: s3://{BUCKET}/plans/{RUN_ID}/plan.json"


@pytest.fixture
def sns(monkeypatch, sns_stub):
    monkeypatch.setattr(notifier, "build_dependencies", lambda: (None, sns_stub[0], None))
    yield sns_stub[1]
    sns_stub[1].assert_no_pending_responses()


def expect_publish(sns, subject: str, message: str) -> None:
    expect_subscriptions(sns, TOPIC_ARN)
    sns.add_response(
        "publish",
        {"MessageId": MESSAGE_ID},
        {"TopicArn": TOPIC_ARN, "Subject": subject, "Message": message},
    )


def result_event(**overrides) -> dict:
    event = {
        "mode": "result",
        "detect": {"runId": RUN_ID},
        "apply": {
            "deleted": 1,
            "failed": 1,
            "skipped": 1,
            "items": [
                {
                    "resourceId": "nat-1",
                    "kind": "nat-gateway",
                    "region": "us-east-1",
                    "outcome": "DELETED",
                    "reason": "deleted",
                },
                {
                    "resourceId": "vol-1",
                    "kind": "ebs-volume",
                    "region": "us-west-2",
                    "outcome": "FAILED",
                    "reason": "VolumeInUse:\nattached",
                },
                {
                    "resourceId": "eip-1",
                    "kind": "elastic-ip",
                    "region": "us-east-1",
                    "outcome": "SKIPPED",
                    "reason": "",
                },
            ],
        },
        "approval": {
            "approvedBy": {
                "mode": "link",
                "ip": "203.0.113.7",
                "userAgent": "Mozilla/5.0",
                "at": "2026-11-03T10:00:00+00:00",
            }
        },
    }
    event.update(overrides)
    return event


def test_result_message_lists_items_and_approver(sns):
    expect_publish(
        sns,
        "[Lab Janitor] Deletions applied: 1 deleted, 1 failed, 1 skipped",
        "\n".join(
            [
                f"Run: {RUN_ID}",
                "Deleted: 1  Failed: 1  Skipped: 1",
                "",
                "DELETED [nat-gateway] nat-1 (us-east-1): deleted",
                "FAILED [ebs-volume] vol-1 (us-west-2): VolumeInUse: attached",
                "SKIPPED [elastic-ip] eip-1 (us-east-1)",
                "",
                "Approved by: link 203.0.113.7 Mozilla/5.0 at 2026-11-03T10:00:00+00:00",
                POINTER,
            ]
        ),
    )

    assert notifier.handler(result_event(), None) == {"published": True, "messageId": MESSAGE_ID}


def test_result_approver_in_iam_mode_shows_the_principal(sns):
    approved_by = {
        "mode": "iam",
        "principal": "arn:aws:iam::123456789012:role/admin",
        "at": "2026-11-03T10:00:00+00:00",
    }
    event = result_event(approval={"approvedBy": approved_by})
    event["apply"]["items"] = []
    expect_publish(
        sns,
        "[Lab Janitor] Deletions applied: 1 deleted, 1 failed, 1 skipped",
        "\n".join(
            [
                f"Run: {RUN_ID}",
                "Deleted: 1  Failed: 1  Skipped: 1",
                "",
                "Approved by: iam arn:aws:iam::123456789012:role/admin "
                "at 2026-11-03T10:00:00+00:00",
                POINTER,
            ]
        ),
    )

    notifier.handler(event, None)


def test_result_without_approver_says_unknown(sns):
    event = result_event(approval={})
    event["apply"] = {"deleted": 0, "failed": 0, "skipped": 0, "items": []}
    expect_publish(
        sns,
        "[Lab Janitor] Deletions applied: 0 deleted, 0 failed, 0 skipped",
        f"Run: {RUN_ID}\nDeleted: 0  Failed: 0  Skipped: 0\n\nApproved by: unknown\n{POINTER}",
    )

    notifier.handler(event, None)


def test_result_subject_falls_back_when_too_long(sns):
    event = result_event()
    event["apply"] = {"deleted": 10**80, "failed": 0, "skipped": 0, "items": []}
    expect_publish(
        sns,
        "[Lab Janitor] Deletions applied",
        f"Run: {RUN_ID}\nDeleted: {10**80}  Failed: 0  Skipped: 0\n\n"
        f"Approved by: link 203.0.113.7 Mozilla/5.0 at 2026-11-03T10:00:00+00:00\n{POINTER}",
    )

    notifier.handler(event, None)


def test_expired_and_rejected_subjects(sns):
    expect_publish(
        sns,
        f"[Lab Janitor] Approval expired: run {RUN_ID}",
        "\n".join(
            [
                f"Run: {RUN_ID}",
                "Nobody approved the plan before the approval window closed.",
                "Nothing was deleted. The next weekly run will detect resources again.",
                POINTER,
            ]
        ),
    )
    expect_publish(
        sns,
        f"[Lab Janitor] Approval rejected: run {RUN_ID}",
        f"Run: {RUN_ID}\nThe plan was rejected. Nothing was deleted.\n{POINTER}",
    )

    event = {"detect": {"runId": RUN_ID}, "error": {"Error": "States.Timeout"}}
    assert notifier.handler({"mode": "expired", **event}, None)["published"] is True
    assert notifier.handler({"mode": "rejected", **event}, None)["published"] is True


def test_rejected_includes_rejecter_when_cause_is_json(sns):
    cause = json.dumps(
        {
            "rejectedBy": {
                "mode": "iam",
                "principal": "arn:aws:iam::123456789012:user/mateo",
                "at": "2026-11-03T10:00:00+00:00",
            }
        }
    )
    expect_publish(
        sns,
        f"[Lab Janitor] Approval rejected: run {RUN_ID}",
        "\n".join(
            [
                f"Run: {RUN_ID}",
                "The plan was rejected. Nothing was deleted.",
                "Rejected by: iam arn:aws:iam::123456789012:user/mateo "
                "at 2026-11-03T10:00:00+00:00",
                POINTER,
            ]
        ),
    )
    event = {
        "mode": "rejected",
        "detect": {"runId": RUN_ID},
        "error": {"Error": "ApprovalRejected", "Cause": cause},
    }

    notifier.handler(event, None)


def test_rejected_accepts_a_plain_string_rejecter(sns):
    expect_publish(
        sns,
        f"[Lab Janitor] Approval rejected: run {RUN_ID}",
        f"Run: {RUN_ID}\nThe plan was rejected. Nothing was deleted.\n"
        f"Rejected by: mateo\n{POINTER}",
    )
    event = {
        "mode": "rejected",
        "detect": {"runId": RUN_ID},
        "error": {"Error": "ApprovalRejected", "Cause": '{"rejectedBy": "mateo"}'},
    }

    notifier.handler(event, None)


@pytest.mark.parametrize(
    "error",
    [
        None,
        {},
        {"Error": "ApprovalRejected"},
        {"Error": "ApprovalRejected", "Cause": "not json"},
        {"Error": "ApprovalRejected", "Cause": "[1, 2]"},
        {"Error": "ApprovalRejected", "Cause": '{"other": 1}'},
        {"Error": "ApprovalRejected", "Cause": '{"rejectedBy": 5}'},
    ],
)
def test_rejected_never_raises_on_a_malformed_cause(sns, error):
    expect_publish(
        sns,
        f"[Lab Janitor] Approval rejected: run {RUN_ID}",
        f"Run: {RUN_ID}\nThe plan was rejected. Nothing was deleted.\n{POINTER}",
    )
    event = {"mode": "rejected", "detect": {"runId": RUN_ID}, "error": error}

    assert notifier.handler(event, None)["published"] is True


def test_result_with_thousands_of_items_stays_under_the_email_limit(sns, monkeypatch):
    # SNS rejects a message over 256 KB; that failure would come after the deletions.
    items = [
        {
            "resourceId": f"snap-{index:017x}",
            "kind": "ebs-snapshot",
            "region": "us-east-1",
            "outcome": "DELETED",
            "reason": "deleted",
        }
        for index in range(5000)
    ]
    event = result_event(apply={"deleted": 5000, "failed": 0, "skipped": 0, "items": items})
    published = []
    expect_subscriptions(sns, TOPIC_ARN)

    def capture(**params):
        published.append(params)
        return {"MessageId": MESSAGE_ID}

    monkeypatch.setattr(sns.client, "publish", capture)

    assert notifier.handler(event, None) == {"published": True, "messageId": MESSAGE_ID}

    (params,) = published
    message = params["Message"]
    lines = message.split("\n")
    assert len(message.encode()) <= 150_000
    # The head and the tail survive; only item lines are dropped.
    assert lines[:2] == [f"Run: {RUN_ID}", "Deleted: 5000  Failed: 0  Skipped: 0"]
    assert lines[3] == f"DELETED [ebs-snapshot] snap-{0:017x} (us-east-1): deleted"
    assert lines[-2:] == [
        "Approved by: link 203.0.113.7 Mozilla/5.0 at 2026-11-03T10:00:00+00:00",
        POINTER,
    ]
    kept = [line for line in lines if line.startswith("DELETED [")]
    notice = f"... truncated, {5000 - len(kept)} lines omitted. Full report: "
    assert 0 < len(kept) < 5000
    assert f"{notice}audit table items under RUN#{RUN_ID}" in message


@pytest.mark.parametrize("mode", ["result", "expired", "rejected"])
def test_outcome_publishes_nothing_without_a_subscriber(sns, mode):
    expect_subscriptions(sns, TOPIC_ARN, PENDING_SUBSCRIPTION)

    with pytest.raises(delivery.NoConfirmedSubscriberError):
        notifier.handler(result_event(mode=mode), None)
