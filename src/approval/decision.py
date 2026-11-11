"""The claim-then-callback sequence that turns one human decision into a resumed execution.

The caller has already parsed the request and, in link mode, checked the typed
confirmation, so a refused request never writes.

1. ``claim``: PENDING -> DECIDING (conditional), so two decisions cannot both win.
2. Resume Step Functions with the task token: ``SendTaskSuccess`` on approve,
   ``SendTaskFailure`` (error ``Rejected``) on reject. ``TaskTimedOut`` marks the
   approval EXPIRED; any other ``ClientError`` (the request was refused, so not
   delivered) reverts the claim so the human can retry (500). A ``BotoCoreError``
   (such as a read timeout) leaves the claim DECIDING and answers 504: the
   callback may have been delivered, so reverting could let a second decision
   race the first, and telling the human "not recorded" could be a lie.
3. ``finalize``: DECIDING -> APPROVED / REJECTED. Once the callback was
   delivered the decision stands, so a failed finalize, whatever the error, is
   logged and not returned.
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
# HTTP 504: Step Functions did not answer, so nobody knows if the decision arrived.
UNKNOWN_OUTCOME = 504


@dataclass(frozen=True)
class Decision:
    """One decision request: ``decision`` is ``approve`` or ``reject``; ``now`` is ISO UTC."""

    approval_id: str
    decision: str
    approver: dict
    now: str


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
    _record, token = found
    approval_id, now = request.approval_id, request.now
    try:
        store.claim(approval_id, now, request.approver)
    except AlreadyDecidedError as error:
        # The status the claim lost to, not the one read earlier: the state
        # machine may have expired the approval in between.
        log.info("approval no longer pending, decision refused")
        return 410 if error.status == EXPIRED else 409
    except ExpiredError:
        log.info("approval expired, decision refused")
        return 410
    # Only error codes and class names are logged: Step Functions messages can echo the token.
    try:
        _resume(states, token, request)
    except BotoCoreError as error:
        # The request may have reached Step Functions, so the claim stays DECIDING.
        log.error(
            "decision callback outcome unknown (%s), approval left DECIDING",
            type(error).__name__,
        )
        return UNKNOWN_OUTCOME
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code")
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
    except ClientError as error:
        log.error(
            "decision delivered but the approval was not finalized (code=%s)",
            error.response.get("Error", {}).get("Code"),
        )
    except BotoCoreError as error:
        log.error(
            "decision delivered but the approval was not finalized (%s)", type(error).__name__
        )
    log.info("approval decided (status=%s, mode=%s)", status, request.approver["mode"])
    return 200
