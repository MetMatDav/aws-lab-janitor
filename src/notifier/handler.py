"""Notifier Lambda: the only component that sends email.

The Step Functions state machine invokes it with a ``mode``. Plan 1 has a
single mode, ``report``, which publishes the weekly report of a detector run to
the reports SNS topic. The ``approval`` and ``result`` modes arrive in Plan 2.
"""

import logging
import os

import boto3
from botocore.client import BaseClient

from shared.plan_store import PlanStore
from shared.report import truncate_report

logger = logging.getLogger("notifier")

# Headroom under the 256 KB SNS message limit for the subject, attributes and encoding.
REPORT_EMAIL_LIMIT_BYTES = 150_000
# SNS rejects email subjects of 100 characters or more.
SUBJECT_LIMIT = 100
DEFAULT_REGION = "us-east-1"
SUBJECT_PREFIX = "[Lab Janitor] Weekly report"
DRY_RUN_MARKER = "(DRY RUN)"


def build_dependencies() -> tuple[PlanStore, BaseClient]:
    """Create the plan store and the SNS client for one invocation."""
    region = os.environ.get("AWS_REGION", DEFAULT_REGION)
    store = PlanStore(boto3.client("s3", region_name=region), os.environ["PLANS_BUCKET"])
    return store, boto3.client("sns", region_name=region)


def report_subject(detect: dict) -> str:
    """Email subject for a detector result; falls back to a fixed text if it would be too long."""
    dry_run = detect["dryRun"]
    marker = f" {DRY_RUN_MARKER}" if dry_run else ""
    subject = (
        f"{SUBJECT_PREFIX}: {detect['candidateCount']} candidates, "
        f"~${detect['estimatedMonthlyUsd']:.2f}/month{marker}"
    )
    return subject if len(subject) < SUBJECT_LIMIT else f"{SUBJECT_PREFIX}{marker}"


def publish_report(event: dict) -> dict:
    """Publish the truncated weekly report of the detector run in ``event["detect"]``."""
    detect = event["detect"]
    run_id = detect["runId"]
    log = logging.LoggerAdapter(logger, {"runId": run_id})
    store, sns = build_dependencies()
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


def handler(event: dict, context) -> dict:
    """Lambda entry point: dispatch on ``event["mode"]``."""
    mode = event.get("mode")
    if mode == "report":
        return publish_report(event)
    raise ValueError(f"unknown notifier mode: {mode}")
