"""``reminder`` mode: nudge the approver when an approval is still pending.

The reminder state machine invokes this mode 60 hours after an approval
request went out. The approval record is stamped ``remindedAt`` with a
conditional update, so the email is sent at most once and only while the
approval is still ``PENDING`` and unexpired.
"""

import logging
import os
from datetime import UTC, datetime

from notifier.approval import CONFIRMATION_LENGTH, decision_sections
from notifier.report import fit_subject
from shared.approvals import ApprovalRecord, short_hash
from shared.plan_summary import render_summary

logger = logging.getLogger("notifier")

SUBJECT_PREFIX = "[Lab Janitor] Reminder: approval pending"


def reminder_subject(record: ApprovalRecord) -> str:
    """Email subject for a reminder; falls back to a fixed text if it would be too long."""
    return fit_subject(f"{SUBJECT_PREFIX}, run {record.run_id}", SUBJECT_PREFIX)


def reminder_message(
    record: ApprovalRecord, approval_url: str, auth: str, confirmation: str
) -> str:
    """The reminder body: the same way to decide as the request, the expiry and the plan."""
    return "\n\n".join(
        "\n".join(lines)
        for lines in [
            [
                f"Reminder: the deletion plan of run {record.run_id} "
                "is still waiting for a decision."
            ],
            *decision_sections(approval_url, record.approval_id, auth, confirmation),
            [
                f"This request expires at {record.expires_at} (UTC). "
                "After that nothing is deleted for this run."
            ],
            [f"Plan sha256: {record.plan_sha256}"],
            [render_summary(record.plan_summary)],
        ]
    )


def send_reminder(event: dict, now: datetime | None = None) -> dict:
    """Email the approvals topic if the approval in ``event`` is still waiting, at most once."""
    from notifier.handler import build_dependencies  # noqa: PLC0415 - handler imports this module

    approval_id = event["approvalId"]
    _store, sns, approvals = build_dependencies()
    record = approvals.mark_reminded(approval_id, (now or datetime.now(UTC)).isoformat())
    if record is None:
        logger.info(
            "reminder skipped: approval decided, expired or already reminded (approval=%s)",
            short_hash(approval_id),
        )
        return {"reminded": False}

    log = logging.LoggerAdapter(logger, {"runId": record.run_id})
    response = sns.publish(
        TopicArn=os.environ["APPROVALS_TOPIC_ARN"],
        Subject=reminder_subject(record),
        Message=reminder_message(
            record,
            os.environ["APPROVAL_URL"],
            os.environ["APPROVAL_AUTH"],
            record.run_id[-CONFIRMATION_LENGTH:],
        ),
    )
    log.info(
        "approval reminder sent (approval=%s, messageId=%s)",
        short_hash(approval_id),
        response["MessageId"],
    )
    return {"reminded": True, "messageId": response["MessageId"]}
