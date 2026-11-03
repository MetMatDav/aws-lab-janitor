"""``approval`` mode: record a pending approval and email the owner how to decide."""

import logging
import os
from datetime import UTC, datetime

from notifier import report
from notifier.report import fit_subject
from shared.approvals import (
    PENDING,
    ApprovalExistsError,
    ApprovalRecord,
    expiry_for,
    short_hash,
)
from shared.plan_store import PlanIntegrityError, plan_key
from shared.plan_summary import render_summary, summarize_plan

logger = logging.getLogger("notifier")

SUBJECT_PREFIX = "[Lab Janitor] Approval needed"
AUTH_MODES = ("link", "iam", "both")
CONFIRMATION_LENGTH = 4


def approval_subject(summary: dict) -> str:
    """Email subject for an approval request; falls back to a fixed text if it would be too long."""
    subject = (
        f"{SUBJECT_PREFIX}: {summary['candidateCount']} deletions, "
        f"~${summary['estimatedMonthlyUsd']:.2f}/month, run {summary['runId']}"
    )
    return fit_subject(subject, SUBJECT_PREFIX)


def _link_lines(approval_url: str, approval_id: str, confirmation: str) -> list[str]:
    return [
        f"Review and decide: {approval_url}/approvals/{approval_id}",
        f"Type the confirmation code {confirmation} on that page.",
    ]


def _iam_lines(approval_url: str, approval_id: str) -> list[str]:
    command = f"python scripts/approve.py --url {approval_url} {approval_id}"
    return ["Approve with your AWS identity:", f"{command} approve", f"{command} reject"]


def approval_message(
    record: ApprovalRecord, summary: dict, approval_url: str, auth: str, confirmation: str
) -> str:
    """The approval email body for the ``link``, ``iam`` or ``both`` approval modes."""
    if auth not in AUTH_MODES:
        raise ValueError(f"unknown approval auth mode: {auth}")
    sections = []
    if auth in ("link", "both"):
        sections.append(_link_lines(approval_url, record.approval_id, confirmation))
    if auth in ("iam", "both"):
        sections.append(_iam_lines(approval_url, record.approval_id))
    return "\n\n".join(
        "\n".join(lines)
        for lines in [
            [
                f"Lab Janitor wants to delete {summary['candidateCount']} resources "
                f"(~${summary['estimatedMonthlyUsd']:.2f}/month)."
            ],
            *sections,
            [f"This request expires at {record.expires_at} (UTC)."],
            [f"Plan sha256: {record.plan_sha256}"],
            [render_summary(summary)],
            [f"Full plan: s3://{os.environ['PLANS_BUCKET']}/{plan_key(record.run_id)}"],
        ]
    )


def request_approval(event: dict, now: datetime | None = None) -> dict:
    """Verify the plan, store a PENDING approval holding the task token and email the owner.

    Raises ``PlanIntegrityError`` before writing or sending anything if the plan
    in S3 does not hash to the sha256 the detector reported.
    """
    from notifier.handler import build_dependencies  # noqa: PLC0415 - handler imports this module

    detect = event["detect"]
    run_id = detect["runId"]
    log = logging.LoggerAdapter(logger, {"runId": run_id})
    store, sns, approvals = build_dependencies()

    plan, sha256 = store.get_plan(run_id, detect["planVersionId"])
    if sha256 != detect["planSha256"]:
        raise PlanIntegrityError(
            f"plan {run_id} hashes to {sha256}, the detector reported {detect['planSha256']}"
        )

    summary = summarize_plan(plan)
    created = now or datetime.now(UTC)
    expires = expiry_for(created)
    record = ApprovalRecord(
        approval_id=event["approvalId"],
        run_id=run_id,
        plan_version_id=detect["planVersionId"],
        plan_sha256=sha256,
        plan_summary=summary,
        status=PENDING,
        created_at=created.isoformat(),
        expires_at=expires.isoformat(),
        approved_by=None,
        decided_at=None,
    )
    try:
        approvals.create(record, event["taskToken"])
    except ApprovalExistsError:
        # A retried task attempt carries a new task token; the stored one would be stale.
        approvals.refresh_token(record.approval_id, event["taskToken"])
        log.info(
            "existing approval request refreshed (approval=%s)", short_hash(record.approval_id)
        )

    message = approval_message(
        record,
        summary,
        os.environ["APPROVAL_URL"],
        os.environ["APPROVAL_AUTH"],
        run_id[-CONFIRMATION_LENGTH:],
    )
    response = sns.publish(
        TopicArn=os.environ["APPROVALS_TOPIC_ARN"],
        Subject=approval_subject(summary),
        Message=message,
    )
    log.info(
        "approval requested (approval=%s, messageId=%s)",
        short_hash(record.approval_id),
        response["MessageId"],
    )
    report.publish_report(event)
    return {
        "approvalId": record.approval_id,
        "expiresAt": record.expires_at,
        "messageId": response["MessageId"],
    }
