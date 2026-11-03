import io

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from notifier import handler as notifier
from shared.plan_store import PlanStore

BUCKET = "test-plans"
TOPIC_ARN = "arn:aws:sns:us-east-1:123456789012:reports"
RUN_ID = "20261102T120000Z-abc123"
REPORT_KEY = f"plans/{RUN_ID}/report.txt"
REPORT_VERSION = "report-v1"
MESSAGE_ID = "11111111-2222-3333-4444-555555555555"


def detect_result(**overrides) -> dict:
    values = {
        "runId": RUN_ID,
        "planKey": f"plans/{RUN_ID}/plan.json",
        "planVersionId": "plan-v1",
        "planSha256": "0" * 64,
        "reportKey": REPORT_KEY,
        "reportVersionId": REPORT_VERSION,
        "candidateCount": 3,
        "errorCount": 0,
        "findingCount": 5,
        "estimatedMonthlyUsd": 42.5,
        "dryRun": True,
    }
    values.update(overrides)
    return values


def report_event(**overrides) -> dict:
    return {
        "mode": "report",
        "detect": detect_result(**overrides),
        "config": {"dryRun": True, "minAgeDays": 7},
    }


@pytest.fixture
def s3_stub():
    client = boto3.client("s3", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.activate()
    yield client, stubber
    stubber.deactivate()


@pytest.fixture
def sns_stub():
    client = boto3.client("sns", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.activate()
    yield client, stubber
    stubber.deactivate()


def install(monkeypatch, s3_stub, sns_stub, report: str) -> None:
    """Make ``build_dependencies`` return stubbed S3 and SNS clients serving ``report``."""
    s3_client, s3_stubber = s3_stub
    body = report.encode()
    s3_stubber.add_response(
        "get_object",
        {"Body": StreamingBody(io.BytesIO(body), len(body))},
        {"Bucket": BUCKET, "Key": REPORT_KEY, "VersionId": REPORT_VERSION},
    )
    monkeypatch.setattr(
        notifier, "build_dependencies", lambda: (PlanStore(s3_client, BUCKET), sns_stub[0])
    )


def expect_publish(sns_stub, subject: str, message: str) -> None:
    sns_stub[1].add_response(
        "publish",
        {"MessageId": MESSAGE_ID},
        {"TopicArn": TOPIC_ARN, "Subject": subject, "Message": message},
    )


def test_report_mode_publishes_truncated_report(monkeypatch, s3_stub, sns_stub):
    report = "\n".join(f"[nat-gateway] nat-{i:06d} (us-east-1) 30d ~$32.85/mo" for i in range(7000))
    assert len(report.encode()) > 300_000
    install(monkeypatch, s3_stub, sns_stub, report)
    captured: dict = {}
    original_publish = sns_stub[0].publish

    def spy(**kwargs):
        captured.update(kwargs)
        return original_publish(**kwargs)

    sns_stub[1].add_response("publish", {"MessageId": MESSAGE_ID})
    monkeypatch.setattr(sns_stub[0], "publish", spy)

    result = notifier.handler(report_event(), None)

    assert result == {"published": True, "messageId": MESSAGE_ID}
    message = captured["Message"]
    assert len(message.encode()) < 150_000
    assert message.endswith(f"s3://{BUCKET}/{REPORT_KEY}")
    assert captured["TopicArn"] == TOPIC_ARN
    s3_stub[1].assert_no_pending_responses()
    sns_stub[1].assert_no_pending_responses()


def test_short_report_is_published_unchanged(monkeypatch, s3_stub, sns_stub):
    install(monkeypatch, s3_stub, sns_stub, "LAB JANITOR WEEKLY REPORT\nnothing to see")
    expect_publish(
        sns_stub,
        "[Lab Janitor] Weekly report: 3 candidates, ~$42.50/month (DRY RUN)",
        "LAB JANITOR WEEKLY REPORT\nnothing to see",
    )

    assert notifier.handler(report_event(), None) == {"published": True, "messageId": MESSAGE_ID}


def test_subject_marks_dry_run_and_stays_short():
    subject = notifier.report_subject(detect_result(dryRun=True))

    assert subject == "[Lab Janitor] Weekly report: 3 candidates, ~$42.50/month (DRY RUN)"
    assert len(subject) < 100


def test_subject_for_live_run_has_no_dry_run_marker():
    subject = notifier.report_subject(detect_result(dryRun=False, estimatedMonthlyUsd=7))

    assert subject == "[Lab Janitor] Weekly report: 3 candidates, ~$7.00/month"


@pytest.mark.parametrize(
    ("dry_run", "expected"),
    [
        (True, "[Lab Janitor] Weekly report (DRY RUN)"),
        (False, "[Lab Janitor] Weekly report"),
    ],
)
def test_long_subject_falls_back(dry_run, expected):
    subject = notifier.report_subject(detect_result(dryRun=dry_run, estimatedMonthlyUsd=1e90))

    assert subject == expected


def test_dry_run_comes_from_detect_result_not_config(monkeypatch, s3_stub, sns_stub):
    install(monkeypatch, s3_stub, sns_stub, "report")
    expect_publish(sns_stub, "[Lab Janitor] Weekly report: 3 candidates, ~$42.50/month", "report")
    event = report_event(dryRun=False)
    event["config"]["dryRun"] = True

    assert notifier.handler(event, None)["published"] is True


@pytest.mark.parametrize("event", [{"mode": "approval"}, {"mode": "bogus"}, {}])
def test_unknown_mode_raises(event):
    with pytest.raises(ValueError, match="unknown notifier mode"):
        notifier.handler(event, None)
