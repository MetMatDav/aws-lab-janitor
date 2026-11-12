"""Requests, records and Stubber expectations for the approval endpoint tests."""

import base64
import json
from datetime import UTC, datetime
from urllib.parse import urlencode

from approval import handler as approval
from shared.approvals import ApprovalRecord
from shared.dynamo import serialize
from tests.adversarial.helpers import ACCOUNT, APPROVAL_ID, RUN_ID, TASK_TOKEN

TABLE = "test-audit"
CONFIRMATION = RUN_ID[-4:]
NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
NOW_ISO = NOW.isoformat()
EXPIRES_AT = "2026-11-06T08:30:00+00:00"
SOURCE_IP = "198.51.100.7"
USER_AGENT = "Mozilla/5.0 (test)"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/Approver"
CALLER_ARN = f"arn:aws:sts::{ACCOUNT}:assumed-role/Approver/session"
LINK_ROUTE = "/approvals/{id}"
IAM_ROUTE = "/iam/approvals/{id}"
LINK_APPROVER = {"mode": "link", "ip": SOURCE_IP, "userAgent": USER_AGENT, "at": NOW_ISO}
IAM_APPROVER = {"mode": "iam", "principal": CALLER_ARN, "at": NOW_ISO}


def key(approval_id: str = APPROVAL_ID) -> dict:
    return {"pk": {"S": f"APPROVAL#{approval_id}"}, "sk": {"S": "META"}}


def summary(**overrides) -> dict:
    values = {
        "runId": RUN_ID,
        "generatedAt": "2026-11-02T12:00:00+00:00",
        "candidateCount": 1,
        "estimatedMonthlyUsd": 0.8,
        "errorCount": 0,
        "regions": ["us-east-1"],
        "candidates": [
            {
                "region": "us-east-1",
                "kind": "ebs-volume",
                "resourceId": "vol-1",
                "reason": "tag Purpose=lab, available, 42 days old",
                "estimatedMonthlyUsd": 0.8,
            }
        ],
        "truncated": False,
    }
    values.update(overrides)
    return values


def record_item(status: str = "PENDING", **overrides) -> dict:
    item = {
        "pk": f"APPROVAL#{APPROVAL_ID}",
        "sk": "META",
        "approvalId": APPROVAL_ID,
        "runId": RUN_ID,
        "planVersionId": "plan-v1",
        "planSha256": "ab" * 32,
        "planSummary": summary(),
        "taskToken": TASK_TOKEN,
        "status": status,
        "createdAt": "2026-11-03T08:00:00+00:00",
        "expiresAt": EXPIRES_AT,
        "ttl": 1800000000,
    }
    item.update(overrides)
    return serialize(item)


def approval_record(**overrides) -> ApprovalRecord:
    """The PENDING record of ``record_item`` as the dataclass the renderers take."""
    values = {
        "approval_id": APPROVAL_ID,
        "run_id": RUN_ID,
        "plan_version_id": "plan-v1",
        "plan_sha256": "ab" * 32,
        "plan_summary": summary(),
        "status": "PENDING",
        "created_at": "2026-11-03T08:00:00+00:00",
        "expires_at": EXPIRES_AT,
        "approved_by": None,
        "decided_at": None,
    }
    values.update(overrides)
    return ApprovalRecord(**values)


def http_event(  # noqa: PLR0913 - one keyword per part of the request an attacker controls
    method: str,
    route: str,
    path_id: str | None = APPROVAL_ID,
    *,
    body: str | None = None,
    base64_body: bool = False,
    iam_arn: str | None = None,
    headers: dict | None = None,
    query: str = "",
) -> dict:
    """An API Gateway HTTP API (payload format 2.0) event."""
    path = route.replace("{id}", path_id or "")
    event = {
        "version": "2.0",
        "routeKey": f"{method} {route}",
        "rawPath": path,
        "rawQueryString": query,
        "headers": {"user-agent": USER_AGENT, **(headers or {})},
        "requestContext": {
            "http": {"method": method, "path": path, "sourceIp": SOURCE_IP, "userAgent": USER_AGENT}
        },
        "isBase64Encoded": False,
    }
    if query:
        event["queryStringParameters"] = dict(pair.split("=", 1) for pair in query.split("&"))
    if path_id is not None:
        event["pathParameters"] = {"id": path_id}
    if iam_arn is not None:
        event["requestContext"]["authorizer"] = {"iam": {"userArn": iam_arn}}
    if body is not None:
        event["body"] = base64.b64encode(body.encode()).decode() if base64_body else body
        event["isBase64Encoded"] = base64_body
    return event


def form(decision: str = "approve", confirm: str | None = CONFIRMATION) -> str:
    fields = {"decision": decision}
    if confirm is not None:
        fields["confirm"] = confirm
    return urlencode(fields)


def get_params(approval_id: str = APPROVAL_ID) -> dict:
    return {"TableName": TABLE, "Key": key(approval_id), "ConsistentRead": True}


def claim_params(approver: dict) -> dict:
    return {
        "TableName": TABLE,
        "Key": key(),
        "UpdateExpression": "SET #s = :deciding, approvedBy = :by",
        "ConditionExpression": "#s = :pending AND expiresAt > :now",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": serialize(
            {":deciding": "DECIDING", ":pending": "PENDING", ":now": NOW_ISO, ":by": approver}
        ),
        "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
    }


def finalize_params(status: str) -> dict:
    return {
        "TableName": TABLE,
        "Key": key(),
        "UpdateExpression": "SET #s = :status, decidedAt = :at",
        "ConditionExpression": "#s = :deciding",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {
            ":status": {"S": status},
            ":at": {"S": NOW_ISO},
            ":deciding": {"S": "DECIDING"},
        },
    }


def success_params(approver: dict) -> dict:
    output = {"decision": "approved", "approvalId": APPROVAL_ID, "approvedBy": approver}
    return {"taskToken": TASK_TOKEN, "output": json.dumps(output)}


def failure_params(approver: dict) -> dict:
    cause = {"approvalId": APPROVAL_ID, "rejectedBy": approver}
    return {"taskToken": TASK_TOKEN, "error": "Rejected", "cause": json.dumps(cause)}


def call(event: dict) -> dict:
    return approval.handler(event, None, now=NOW)
