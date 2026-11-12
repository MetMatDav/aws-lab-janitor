"""``report`` mode: publish the weekly report of a detector run to the reports topic."""

import logging
import os

from botocore.client import BaseClient

from notifier.delivery import require_subscriber
from shared.plan_store import PlanStore
from shared.report import truncate_report

logger = logging.getLogger("notifier")

# Headroom under the 256 KB SNS message limit for the subject, attributes and encoding.
REPORT_EMAIL_LIMIT_BYTES = 150_000
# SNS rejects email subjects of 100 characters or more.
SUBJECT_LIMIT = 100
SUBJECT_PREFIX = "[Lab Janitor] Weekly report"
DRY_RUN_MARKER = "(DRY RUN)"


def fit_subject(subject: str, fallback: str) -> str:
    """``subject`` if SNS accepts it, else the shorter ``fallback``."""
    return subject if len(subject) < SUBJECT_LIMIT else fallback


def report_subject(detect: dict) -> str:
    """Email subject for a detector result; falls back to a fixed text if it would be too long."""
    dry_run = detect["dryRun"]
    marker = f" {DRY_RUN_MARKER}" if dry_run else ""
    subject = (
        f"{SUBJECT_PREFIX}: {detect['candidateCount']} candidates, "
        f"~${detect['estimatedMonthlyUsd']:.2f}/month{marker}"
    )
    return fit_subject(subject, f"{SUBJECT_PREFIX}{marker}")


def publish_report(event: dict) -> dict:
    """Publish the truncated weekly report of the detector run in ``event["detect"]``."""
    from notifier.handler import build_dependencies  # noqa: PLC0415 - handler imports this module

    store, sns, _approvals = build_dependencies()
    require_subscriber(sns, os.environ["REPORTS_TOPIC_ARN"])
    return send_report(event, store, sns)


def send_report(event: dict, store: PlanStore, sns: BaseClient) -> dict:
    """Publish the report; the caller has checked that the reports topic has a subscriber."""
    detect = event["detect"]
    run_id = detect["runId"]
    log = logging.LoggerAdapter(logger, {"runId": run_id})
    report = store.get_report(run_id, detect["reportVersionId"])
    pointer = f"s3://{os.environ['PLANS_BUCKET']}/{detect['reportKey']}"
    message = truncate_report(report, REPORT_EMAIL_LIMIT_BYTES, pointer)
    response = sns.publish(
        TopicArn=os.environ["REPORTS_TOPIC_ARN"],
        Subject=report_subject(detect),
        Message=message,
    )
    log.info("weekly report published (messageId=%s)", response["MessageId"])
    return {"published": True, "messageId": response["MessageId"]}
