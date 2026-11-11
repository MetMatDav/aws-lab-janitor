"""Approval endpoint: API Gateway HTTP API (payload format 2.0) in front of one Lambda.

Possession of the approval link is authority to approve, so the handler is
strict about ordering: ``GET`` never writes; a ``POST`` is parsed and checked
(the IAM caller, or the link's typed confirmation) before any write; the record is claimed
(PENDING -> DECIDING) before Step Functions is resumed, so two decisions cannot
both win; and the claim is reverted, expired or finalized depending on how the
callback went (see ``approval.decision``). The approval id and the task token
are never logged: log lines carry ``approvalIdHash`` instead.
"""

import base64
import binascii
import json
import logging
import os
import secrets
from datetime import UTC, datetime
from urllib.parse import parse_qs

import boto3
from botocore.client import BaseClient

from approval import page
from approval.decision import DECISIONS, Decision, decide
from shared.approvals import EXPIRED, PENDING, ApprovalRecord, ApprovalStore, short_hash
from shared.plan_summary import render_summary

logger = logging.getLogger("approval")

DEFAULT_REGION = "us-east-1"
GET_ROUTE = "GET /approvals/{id}"
POST_ROUTE = "POST /approvals/{id}"
IAM_ROUTE = "POST /iam/approvals/{id}"
ROUTE_MODES = {
    GET_ROUTE: ("link", "both"),
    POST_ROUTE: ("link", "both"),
    IAM_ROUTE: ("iam", "both"),
}
CONFIRMATION_LENGTH = 4
MAX_FORM_FIELDS = 4

HTML = "text/html; charset=utf-8"
JSON = "application/json"
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
}
STATUS_PAGES = {
    400: ("Invalid request", "The form was incomplete. Go back to the email link and try again."),
    404: ("Not found", "There is no approval at this address."),
    409: ("Already decided", "This approval was already decided. Nothing changed."),
    410: ("Approval expired", "This approval expired. Nothing will be deleted for this run."),
    500: ("Something went wrong", "The decision was not recorded. Try again in a few minutes."),
    504: (
        "Decision status unknown",
        "The decision may have been recorded. "
        "Check the latest emails for this run before trying again.",
    ),
}
WRONG_CONFIRMATION = "The confirmation code did not match. Nothing changed; go back and retry."
ERROR_NAMES = {
    400: "bad request",
    403: "forbidden",
    404: "not found",
    409: "already decided",
    410: "expired",
    500: "internal error",
    504: "unknown outcome",
}


def build_dependencies() -> tuple[ApprovalStore, BaseClient]:
    """Create the approval store and the Step Functions client for one invocation."""
    region = os.environ.get("AWS_REGION", DEFAULT_REGION)
    store = ApprovalStore(boto3.client("dynamodb", region_name=region), os.environ["AUDIT_TABLE"])
    return store, boto3.client("stepfunctions", region_name=region)


def _arn_parts(arn: str) -> tuple[str, str, str, str] | None:
    """``(partition, service, account, resource)`` of an ARN, or ``None`` if it is not one."""
    parts = arn.split(":", 5)
    if len(parts) != 6 or parts[0] != "arn":  # noqa: PLR2004 - an ARN has six fields
        return None
    return parts[1], parts[2], parts[4], parts[5]


def allowed_principal(principal: str, approver_role_arn: str) -> bool:
    """Whether the IAM caller ``principal`` may decide.

    True when the two ARNs are equal, or when ``principal`` is a session of the
    approver role: ``arn:<p>:sts::<account>:assumed-role/<name>/<session>`` in
    the same partition and account as ``arn:<p>:iam::<account>:role/<path/><name>``
    (IAM Identity Center roles live under ``role/aws-reserved/sso.amazonaws.com/...``).
    """
    if not principal or not approver_role_arn:
        return False
    if principal == approver_role_arn:
        return True
    role, caller = _arn_parts(approver_role_arn), _arn_parts(principal)
    if role is None or caller is None:
        return False
    role_partition, role_service, role_account, role_resource = role
    partition, service, account, resource = caller
    if role_service != "iam" or not role_resource.startswith("role/"):
        return False
    if (partition, service, account) != (role_partition, "sts", role_account):
        return False
    segments = resource.split("/")
    role_name = role_resource.rsplit("/", 1)[-1]
    return (
        len(segments) == 3  # noqa: PLR2004 - assumed-role/<name>/<session>
        and segments[0] == "assumed-role"
        and bool(role_name)
        and segments[1] == role_name
    )


def _respond(status: int, content_type: str, body: str) -> dict:
    return {
        "statusCode": status,
        "headers": {**SECURITY_HEADERS, "Content-Type": content_type},
        "body": body,
    }


def _html_status(status: int, text: str | None = None) -> dict:
    title, default_text = STATUS_PAGES[status]
    return _respond(status, HTML, page.render_status(title, text or default_text))


def _json(status: int, payload: dict) -> dict:
    return _respond(status, JSON, json.dumps(payload))


def _error(route: str, status: int) -> dict:
    """An error response in the route's format: HTML for the link routes, JSON otherwise."""
    if route in (GET_ROUTE, POST_ROUTE):
        return _html_status(status)
    return _json(status, {"error": ERROR_NAMES[status]})


def _body(event: dict) -> str | None:
    """The request body as text, or ``None`` if it is not valid base64 / UTF-8."""
    body = event.get("body") or ""
    if not event.get("isBase64Encoded"):
        return body
    try:
        return base64.b64decode(body, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None


def _parse_form(text: str | None) -> tuple[str, str] | None:
    """``(decision, confirm)`` from a form-encoded body, or ``None`` if incomplete or malformed."""
    if text is None:
        return None
    try:
        fields = parse_qs(text, strict_parsing=True, max_num_fields=MAX_FORM_FIELDS)
    except ValueError:
        return None
    decision, confirm = fields.get("decision", []), fields.get("confirm", [])
    if len(decision) != 1 or len(confirm) != 1 or decision[0] not in DECISIONS:
        return None
    return decision[0], confirm[0]


def _parse_json_decision(text: str | None) -> str | None:
    """The decision of a ``{"decision": ...}`` JSON body, or ``None`` if invalid."""
    if text is None:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    decision = payload.get("decision") if isinstance(payload, dict) else None
    return decision if isinstance(decision, str) and decision in DECISIONS else None


def _confirmation_matches(confirm: str, run_id: str) -> bool:
    expected = run_id[-CONFIRMATION_LENGTH:].casefold()
    return secrets.compare_digest(confirm.strip().casefold().encode(), expected.encode())


def _closed_status(record: ApprovalRecord, now: str) -> int | None:
    """410 or 409 if ``record`` no longer accepts a decision, else ``None``. Reads only."""
    if record.status == EXPIRED:
        return 410
    if record.status != PENDING:
        return 409
    if record.expires_at <= now:
        return 410
    return None


def _get_page(event: dict, approval_id: str, now: str, log: logging.LoggerAdapter) -> dict:
    """Read-only review page."""
    store, _ = build_dependencies()
    found = store.get(approval_id)
    if found is None:
        return _html_status(404)
    record, _token = found
    closed = _closed_status(record, now)
    if closed is not None:
        return _html_status(closed)
    log.info("approval page served")
    return _respond(200, HTML, page.render_form(record, render_summary(record.plan_summary)))


def _post_link(event: dict, approval_id: str, now: str, log: logging.LoggerAdapter) -> dict:
    """Decision from the form on the review page, confirmed with the run id suffix.

    A malformed form, a closed approval or a wrong confirmation is refused before
    any write; the claim's own 409/410 still covers a concurrent decision.
    """
    parsed = _parse_form(_body(event))
    if parsed is None:
        log.info("incomplete or malformed decision form")
        return _html_status(400)
    decision, confirm = parsed
    store, states = build_dependencies()
    found = store.get(approval_id)
    if found is None:
        return _html_status(404)
    record = found[0]
    closed = _closed_status(record, now)
    if closed is not None:
        log.info("approval no longer pending, decision refused")
        return _html_status(closed)
    if not _confirmation_matches(confirm, record.run_id):
        log.warning("confirmation code mismatch, decision refused")
        return _html_status(400, WRONG_CONFIRMATION)
    http = event.get("requestContext", {}).get("http", {})
    approver = {
        "mode": "link",
        "ip": http.get("sourceIp", ""),
        "userAgent": http.get("userAgent", ""),
        "at": now,
    }
    status = decide(store, states, found, Decision(approval_id, decision, approver, now), log)
    if status == 200:  # noqa: PLR2004 - HTTP status
        return _respond(200, HTML, page.render_done(decision))
    return _html_status(status)


def _post_iam(event: dict, approval_id: str, now: str, log: logging.LoggerAdapter) -> dict:
    """Decision signed with SigV4; the caller must be the configured approver."""
    authorizer = event.get("requestContext", {}).get("authorizer") or {}
    principal = (authorizer.get("iam") or {}).get("userArn") or ""
    if not allowed_principal(principal, os.environ.get("APPROVER_ROLE_ARN", "")):
        log.warning("caller is not the approver principal, decision refused")
        return _error(IAM_ROUTE, 403)
    decision = _parse_json_decision(_body(event))
    if decision is None:
        log.info("malformed decision body")
        return _error(IAM_ROUTE, 400)
    store, states = build_dependencies()
    found = store.get(approval_id)
    if found is None:
        return _error(IAM_ROUTE, 404)
    approver = {"mode": "iam", "principal": principal, "at": now}
    status = decide(store, states, found, Decision(approval_id, decision, approver, now), log)
    if status == 200:  # noqa: PLR2004 - HTTP status
        return _json(200, {"status": DECISIONS[decision]})
    return _error(IAM_ROUTE, status)


ROUTES = {GET_ROUTE: _get_page, POST_ROUTE: _post_link, IAM_ROUTE: _post_iam}


def handler(event: dict, context, now: datetime | None = None) -> dict:
    """Lambda entry point: route on ``event["routeKey"]`` if ``APPROVAL_AUTH`` serves it."""
    route = event.get("routeKey", "")
    if os.environ.get("APPROVAL_AUTH", "") not in ROUTE_MODES.get(route, ()):
        return _error(route, 404)
    approval_id = (event.get("pathParameters") or {}).get("id")
    if not approval_id:
        return _error(route, 404)
    log = logging.LoggerAdapter(logger, {"approvalIdHash": short_hash(approval_id)})
    timestamp = (now or datetime.now(UTC)).isoformat()
    try:
        return ROUTES[route](event, approval_id, timestamp, log)
    except Exception as error:  # noqa: BLE001 - every failure becomes a 500 page
        # No traceback: chained exception messages can carry the approval id or the token.
        log.error("approval request failed (%s: %s)", type(error).__name__, error, exc_info=False)
        return _error(route, 500)
