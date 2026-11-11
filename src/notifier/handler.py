"""Notifier Lambda: the only component that sends email.

The Step Functions state machine invokes it with a ``mode``: ``report`` (weekly
report), ``approval`` (ask a human to approve the plan), ``result`` (what the
executor did), ``expired`` and ``rejected`` (the approval did not happen). The
reminder state machine invokes it with ``reminder`` (the approval is still
pending after 60 hours).
"""

import os

import boto3
from botocore.client import BaseClient

from notifier import approval, outcomes, reminder, report
from shared.approvals import ApprovalStore
from shared.plan_store import PlanStore

DEFAULT_REGION = "us-east-1"


def build_dependencies() -> tuple[PlanStore, BaseClient, ApprovalStore]:
    """Create the plan store, the SNS client and the approval store for one invocation."""
    region = os.environ.get("AWS_REGION", DEFAULT_REGION)
    store = PlanStore(boto3.client("s3", region_name=region), os.environ["PLANS_BUCKET"])
    approvals = ApprovalStore(
        boto3.client("dynamodb", region_name=region), os.environ["AUDIT_TABLE"]
    )
    return store, boto3.client("sns", region_name=region), approvals


def handler(event: dict, context) -> dict:
    """Lambda entry point: dispatch on ``event["mode"]``."""
    modes = {
        "report": report.publish_report,
        "approval": approval.request_approval,
        "result": outcomes.publish_result,
        "expired": outcomes.publish_expired,
        "rejected": outcomes.publish_rejected,
        "reminder": reminder.send_reminder,
    }
    mode = event.get("mode")
    if mode not in modes:
        raise ValueError(f"unknown notifier mode: {mode}")
    return modes[mode](event)
