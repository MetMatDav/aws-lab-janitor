"""The claim-then-callback sequence that turns one human decision into a resumed execution.

1. ``claim``: PENDING -> DECIDING (conditional), so two decisions cannot both win.
2. Typed confirmation (link mode): a mismatch reverts the claim.
3. Resume Step Functions with the task token: ``SendTaskSuccess`` on approve,
   ``SendTaskFailure`` (error ``Rejected``) on reject. ``TaskTimedOut`` marks the
   approval EXPIRED; any other failure reverts the claim so the human can retry.
4. ``finalize``: DECIDING -> APPROVED / REJECTED.
"""

import json
import logging
from dataclasses import dataclass

from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError

from shared.approvals import (
    APPROVED,
    EXPIRED,
    REJECTED,
    AlreadyDecidedError,
    ApprovalRecord,
    ApprovalStore,
    ExpiredError,
)

DECISIONS = {"approve": APPROVED, "reject": REJECTED}
TASK_TIMED_OUT = "TaskTimedOut"


@dataclass(frozen=True)
class Decision:
    """One decision request: ``decision`` is ``approve`` or ``reject``; ``now`` is ISO UTC."""

    approval_id: str
    decision: str
    approver: dict
    now: str
    confirmed: bool = True


def _resume(states: BaseClient, token: str, request: Decision) -> None:
    if request.decision == "approve":
        output = {
            "decision": "approved",
            "approvalId": request.approval_id,
            "approvedBy": request.approver,
        }
        states.send_task_success(taskToken=token, output=json.dumps(output))
    else:
        cause = {"approvalId": request.approval_id, "rejectedBy": request.approver}
        states.send_task_failure(taskToken=token, error="Rejected", cause=json.dumps(cause))


def decide(
    store: ApprovalStore,
    states: BaseClient,
    found: tuple[ApprovalRecord, str],
    request: Decision,
    log: logging.LoggerAdapter,
) -> int:
    """Run the decision sequence for the record and token in ``found``; return the HTTP status."""
    record, token = found
    approval_id, now = request.approval_id, request.now
    try:
        store.claim(approval_id, now, request.approver)
    except AlreadyDecidedError:
        log.info("approval no longer pending, decision refused")
        return 410 if record.status == EXPIRED else 409
    except ExpiredError:
        log.info("approval expired, decision refused")
        return 410
    if not request.confirmed:
        store.revert(approval_id)
        log.warning("confirmation code mismatch, claim reverted")
        return 400
    try:
        _resume(states, token, request)
    except (ClientError, BotoCoreError) as error:
        # Only the error code is logged: Step Functions error messages can echo the token.
        code = (
            error.response.get("Error", {}).get("Code")
            if isinstance(error, ClientError)
            else type(error).__name__
        )
        if code == TASK_TIMED_OUT:
            store.expire(approval_id, now)
            log.warning("task timed out before the decision, approval expired")
            return 410
        store.revert(approval_id)
        log.error("decision callback failed (code=%s), claim reverted", code)
        return 500
    status = DECISIONS[request.decision]
    try:
        store.finalize(approval_id, status, now)
    except AlreadyDecidedError:
        # The callback was accepted, so the decision stands; only the record is behind.
        log.error("decision delivered but the approval was no longer DECIDING")
    log.info("approval decided (status=%s, mode=%s)", status, request.approver["mode"])
    return 200
