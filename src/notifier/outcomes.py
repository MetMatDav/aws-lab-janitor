"""``result``, ``expired`` and ``rejected`` modes: tell the owner what happened to a plan."""

import json
import logging
import os

from notifier.delivery import require_subscriber
from notifier.report import REPORT_EMAIL_LIMIT_BYTES, fit_subject
from shared.plan_store import plan_key
from shared.report import truncate_report

logger = logging.getLogger("notifier")

RESULT_SUBJECT = "[Lab Janitor] Deletions applied"
EXPIRED_SUBJECT = "[Lab Janitor] Approval expired"
REJECTED_SUBJECT = "[Lab Janitor] Approval rejected"
UNKNOWN_APPROVER = "unknown"


def _plan_pointer(run_id: str) -> str:
    return f"Full plan: s3://{os.environ['PLANS_BUCKET']}/{plan_key(run_id)}"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def describe_approver(who: dict) -> str:
    """``<mode> <principal> <ip> <userAgent> at <at>``, with whichever of those keys are present."""
    tokens = [str(who[key]) for key in ("mode", "principal", "ip", "userAgent") if who.get(key)]
    if who.get("at"):
        tokens.append(f"at {who['at']}")
    return " ".join(tokens) or UNKNOWN_APPROVER


def _item_line(item: dict) -> str:
    line = f"{item['outcome']} [{item['kind']}] {item['resourceId']} ({item['region']})"
    reason = _one_line(item.get("reason") or "")
    return f"{line}: {reason}" if reason else line


def _publish(subject: str, message: str, run_id: str) -> dict:
    from notifier.handler import build_dependencies  # noqa: PLC0415 - handler imports this module

    _store, sns, _approvals = build_dependencies()
    require_subscriber(sns, os.environ["REPORTS_TOPIC_ARN"])
    response = sns.publish(
        TopicArn=os.environ["REPORTS_TOPIC_ARN"], Subject=subject, Message=message
    )
    logging.LoggerAdapter(logger, {"runId": run_id}).info(
        "outcome published (subject=%s, messageId=%s)", subject, response["MessageId"]
    )
    return {"published": True, "messageId": response["MessageId"]}


def publish_result(event: dict) -> dict:
    """Email what the executor did: one line per item and who approved it."""
    run_id = event["detect"]["runId"]
    apply = event["apply"]
    approver = event.get("approval", {}).get("approvedBy")
    subject = fit_subject(
        f"{RESULT_SUBJECT}: {apply['deleted']} deleted, "
        f"{apply['failed']} failed, {apply['skipped']} skipped",
        RESULT_SUBJECT,
    )
    head = [
        f"Run: {run_id}",
        f"Deleted: {apply['deleted']}  Failed: {apply['failed']}  Skipped: {apply['skipped']}",
        "",
    ]
    tail = [
        f"Approved by: {describe_approver(approver) if approver else UNKNOWN_APPROVER}",
        _plan_pointer(run_id),
    ]
    items = "\n".join(_item_line(item) for item in apply["items"])
    if items:
        # A plan can hold thousands of items and SNS refuses a message over
        # 256 KB. Only item lines are dropped: every outcome is in the audit table.
        fixed = len("\n".join([*head, "", *tail]).encode()) + 1
        items = truncate_report(
            items, REPORT_EMAIL_LIMIT_BYTES - fixed, f"audit table items under RUN#{run_id}"
        )
    lines = [*head, *([items, ""] if items else []), *tail]
    return _publish(subject, "\n".join(lines), run_id)


def publish_expired(event: dict) -> dict:
    """Email that nobody approved the plan in time and nothing was deleted."""
    run_id = event["detect"]["runId"]
    message = "\n".join(
        [
            f"Run: {run_id}",
            "Nobody approved the plan before the approval window closed.",
            "Nothing was deleted. The next weekly run will detect resources again.",
            _plan_pointer(run_id),
        ]
    )
    return _publish(
        fit_subject(f"{EXPIRED_SUBJECT}: run {run_id}", EXPIRED_SUBJECT), message, run_id
    )


def _rejecter(error: dict) -> str | None:
    """Who rejected, from a Step Functions error whose ``Cause`` is JSON; ``None`` if unknown."""
    try:
        rejected_by = json.loads(error["Cause"])["rejectedBy"]
    except (KeyError, TypeError, ValueError):
        return None
    if isinstance(rejected_by, str):
        return rejected_by or None
    if isinstance(rejected_by, dict):
        return describe_approver(rejected_by)
    return None


def publish_rejected(event: dict) -> dict:
    """Email that the plan was rejected and nothing was deleted."""
    run_id = event["detect"]["runId"]
    rejecter = _rejecter(event.get("error") or {})
    lines = [f"Run: {run_id}", "The plan was rejected. Nothing was deleted."]
    if rejecter:
        lines.append(f"Rejected by: {rejecter}")
    lines.append(_plan_pointer(run_id))
    subject = fit_subject(f"{REJECTED_SUBJECT}: run {run_id}", REJECTED_SUBJECT)
    return _publish(subject, "\n".join(lines), run_id)
