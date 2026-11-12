import base64
import json
import logging
from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest
from botocore.exceptions import ReadTimeoutError
from botocore.stub import Stubber

from approval import handler as approval
from shared.approvals import ApprovalStore, short_hash
from shared.dynamo import serialize

TABLE = "test-audit"
APPROVAL_ID = "11111111-aaaa-bbbb-cccc-222222222222"
TASK_TOKEN = "task-token-not-a-real-one"  # noqa: S105 - test value
RUN_ID = "20261102T120000Z-abc1"
CONFIRMATION = "abc1"
NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
NOW_ISO = NOW.isoformat()
EXPIRES_AT = "2026-11-06T08:30:00+00:00"
SOURCE_IP = "198.51.100.7"
USER_AGENT = "Mozilla/5.0 (test)"
ACCOUNT = "123456789012"
SSO_ROLE_ARN = (
    f"arn:aws:iam::{ACCOUNT}:role/aws-reserved/sso.amazonaws.com/us-east-1/AWSReservedSSO_Admin_abc"
)
SSO_CALLER_ARN = f"arn:aws:sts::{ACCOUNT}:assumed-role/AWSReservedSSO_Admin_abc/user"
KEY = {"pk": {"S": f"APPROVAL#{APPROVAL_ID}"}, "sk": {"S": "META"}}

LINK_APPROVER = {"mode": "link", "ip": SOURCE_IP, "userAgent": USER_AGENT, "at": NOW_ISO}
IAM_APPROVER = {"mode": "iam", "principal": SSO_CALLER_ARN, "at": NOW_ISO}

GET_ROUTE = "/approvals/{id}"
IAM_ROUTE = "/iam/approvals/{id}"

HARDENING_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"
    ),
}


def summary() -> dict:
    return {
        "runId": RUN_ID,
        "generatedAt": "2026-11-02T12:00:00+00:00",
        "candidateCount": 2,
        "estimatedMonthlyUsd": 34.35,
        "errorCount": 0,
        "regions": ["us-east-1"],
        "candidates": [
            {
                "region": "us-east-1",
                "kind": "nat-gateway",
                "resourceId": "nat-1",
                "reason": "idle <for> 30 days",
                "estimatedMonthlyUsd": 32.85,
            },
            {
                "region": "us-east-1",
                "kind": "eip",
                "resourceId": "eipalloc-1",
                "reason": "unattached",
                "estimatedMonthlyUsd": None,
            },
        ],
        "truncated": False,
    }


def record_item(status: str = "PENDING", expires_at: str = EXPIRES_AT) -> dict:
    return serialize(
        {
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
            "expiresAt": expires_at,
            "ttl": 1800000000,
        }
    )


def http_event(  # noqa: PLR0913 - mirrors the payload fields the tests vary
    method: str,
    route: str,
    path_id: str | None,
    *,
    body: str | None = None,
    iam_arn: str | None = None,
    base64_body: bool = True,
) -> dict:
    """An API Gateway HTTP API (payload format 2.0) event."""
    event = {
        "version": "2.0",
        "routeKey": f"{method} {route}",
        "rawPath": route.replace("{id}", path_id or ""),
        "headers": {"user-agent": USER_AGENT},
        "requestContext": {
            "http": {
                "method": method,
                "path": route.replace("{id}", path_id or ""),
                "sourceIp": SOURCE_IP,
                "userAgent": USER_AGENT,
            },
        },
        "isBase64Encoded": False,
    }
    if path_id is not None:
        event["pathParameters"] = {"id": path_id}
    if iam_arn is not None:
        event["requestContext"]["authorizer"] = {"iam": {"userArn": iam_arn}}
    if body is not None:
        if base64_body:
            event["body"] = base64.b64encode(body.encode()).decode()
            event["isBase64Encoded"] = True
        else:
            event["body"] = body
    return event


def form(decision: str = "approve", confirm: str | None = CONFIRMATION) -> str:
    fields = {"decision": decision}
    if confirm is not None:
        fields["confirm"] = confirm
    return urlencode(fields)


def get_params() -> dict:
    return {"TableName": TABLE, "Key": KEY, "ConsistentRead": True}


def claim_params(approver: dict) -> dict:
    return {
        "TableName": TABLE,
        "Key": KEY,
        "UpdateExpression": "SET #s = :deciding, approvedBy = :by",
        "ConditionExpression": "#s = :pending AND expiresAt > :now",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": serialize(
            {":deciding": "DECIDING", ":pending": "PENDING", ":now": NOW_ISO, ":by": approver}
        ),
        "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
    }


def revert_params() -> dict:
    return {
        "TableName": TABLE,
        "Key": KEY,
        "UpdateExpression": "SET #s = :pending REMOVE approvedBy",
        "ConditionExpression": "#s = :deciding",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":pending": {"S": "PENDING"}, ":deciding": {"S": "DECIDING"}},
    }


def finalize_params(status: str) -> dict:
    return {
        "TableName": TABLE,
        "Key": KEY,
        "UpdateExpression": "SET #s = :status, decidedAt = :at",
        "ConditionExpression": "#s = :deciding",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {
            ":status": {"S": status},
            ":at": {"S": NOW_ISO},
            ":deciding": {"S": "DECIDING"},
        },
    }


def expire_params() -> dict:
    return {
        "TableName": TABLE,
        "Key": KEY,
        "UpdateExpression": "SET #s = :expired, decidedAt = :at",
        "ConditionExpression": "#s IN (:pending, :deciding)",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {
            ":expired": {"S": "EXPIRED"},
            ":at": {"S": NOW_ISO},
            ":pending": {"S": "PENDING"},
            ":deciding": {"S": "DECIDING"},
        },
    }


def success_params(approver: dict) -> dict:
    return {
        "taskToken": TASK_TOKEN,
        "output": json.dumps(
            {"decision": "approved", "approvalId": APPROVAL_ID, "approvedBy": approver}
        ),
    }


def failure_params(approver: dict) -> dict:
    return {
        "taskToken": TASK_TOKEN,
        "error": "Rejected",
        "cause": json.dumps({"approvalId": APPROVAL_ID, "rejectedBy": approver}),
    }


@pytest.fixture
def ddb(stub_factory) -> Stubber:
    return stub_factory("dynamodb")


@pytest.fixture
def sfn(stub_factory) -> Stubber:
    return stub_factory("stepfunctions")


@pytest.fixture
def deps(monkeypatch, ddb, sfn):
    """Stubbed DynamoDB and Step Functions wired into ``build_dependencies``; mode ``both``."""
    monkeypatch.setenv("APPROVAL_AUTH", "both")
    monkeypatch.setenv("APPROVER_ROLE_ARN", SSO_ROLE_ARN)
    monkeypatch.setattr(
        approval, "build_dependencies", lambda: (ApprovalStore(ddb.client, TABLE), sfn.client)
    )
    yield ddb, sfn
    ddb.assert_no_pending_responses()
    sfn.assert_no_pending_responses()


def call(event: dict) -> dict:
    return approval.handler(event, None, now=NOW)


def stub_get(ddb: Stubber, item: dict | None) -> None:
    ddb.add_response("get_item", {"Item": item} if item is not None else {}, get_params())


def stub_claim_failure(ddb: Stubber, approver: dict, old: dict) -> None:
    ddb.add_client_error(
        "update_item",
        service_error_code="ConditionalCheckFailedException",
        http_status_code=400,
        expected_params=claim_params(approver),
        modeled_fields={"Item": old},
    )


def html_body(response: dict) -> str:
    assert response["headers"]["Content-Type"] == "text/html; charset=utf-8"
    return response["body"]


def json_body(response: dict) -> dict:
    assert response["headers"]["Content-Type"] == "application/json"
    return json.loads(response["body"])


# GET /approvals/{id}


def test_get_renders_form_and_writes_nothing(deps):
    ddb, _ = deps
    stub_get(ddb, record_item())

    response = call(http_event("GET", GET_ROUTE, APPROVAL_ID))

    assert response["statusCode"] == 200
    body = html_body(response)
    assert '<form method="post">' in body
    assert RUN_ID in body
    assert "idle &lt;for&gt; 30 days" in body
    assert "~$32.85/mo" in body


def test_get_unknown_id_is_404(deps):
    ddb, _ = deps
    stub_get(ddb, None)

    response = call(http_event("GET", GET_ROUTE, APPROVAL_ID))

    assert response["statusCode"] == 404
    assert "<form" not in html_body(response)


def test_get_expired_record_is_410(deps):
    ddb, _ = deps
    stub_get(ddb, record_item(status="EXPIRED"))

    response = call(http_event("GET", GET_ROUTE, APPROVAL_ID))

    assert response["statusCode"] == 410
    assert "<form" not in html_body(response)


def test_get_pending_record_past_expiry_is_410_without_writing(deps):
    ddb, _ = deps
    stub_get(ddb, record_item(expires_at=NOW_ISO))

    response = call(http_event("GET", GET_ROUTE, APPROVAL_ID))

    assert response["statusCode"] == 410


@pytest.mark.parametrize("status", ["APPROVED", "REJECTED", "DECIDING"])
def test_get_decided_record_is_409(deps, status):
    ddb, _ = deps
    stub_get(ddb, record_item(status=status))

    response = call(http_event("GET", GET_ROUTE, APPROVAL_ID))

    assert response["statusCode"] == 409
    assert "<form" not in html_body(response)


# POST /approvals/{id}


@pytest.mark.parametrize(
    "body",
    [
        None,
        "",
        "decision=approve",
        "confirm=abc1",
        "decision=maybe&confirm=abc1",
        "decision=approve&decision=reject&confirm=abc1",
        "decision=approve&confirm=abc1&confirm=abc1",
    ],
)
def test_post_without_body_is_400_and_writes_nothing(deps, body):
    response = call(http_event("POST", GET_ROUTE, APPROVAL_ID, body=body))

    assert response["statusCode"] == 400
    html_body(response)


def test_post_with_invalid_base64_is_400(deps):
    event = http_event("POST", GET_ROUTE, APPROVAL_ID, body=form(), base64_body=False)
    event["isBase64Encoded"] = True
    event["body"] = "%%%not-base64%%%"

    response = call(event)

    assert response["statusCode"] == 400


def test_post_approve_happy_path(deps):
    ddb, sfn = deps
    stub_get(ddb, record_item())
    ddb.add_response("update_item", {}, claim_params(LINK_APPROVER))
    sfn.add_response("send_task_success", {}, success_params(LINK_APPROVER))
    ddb.add_response("update_item", {}, finalize_params("APPROVED"))

    response = call(http_event("POST", GET_ROUTE, APPROVAL_ID, body=form("approve")))

    assert response["statusCode"] == 200
    assert "approved" in html_body(response).lower()


def test_post_reject_sends_task_failure_with_cause(deps):
    ddb, sfn = deps
    stub_get(ddb, record_item())
    ddb.add_response("update_item", {}, claim_params(LINK_APPROVER))
    sfn.add_response("send_task_failure", {}, failure_params(LINK_APPROVER))
    ddb.add_response("update_item", {}, finalize_params("REJECTED"))

    response = call(
        http_event("POST", GET_ROUTE, APPROVAL_ID, body=form("reject"), base64_body=False)
    )

    assert response["statusCode"] == 200
    assert "rejected" in html_body(response).lower()


def test_post_accepts_confirmation_with_surrounding_whitespace_and_case(deps):
    ddb, sfn = deps
    stub_get(ddb, record_item())
    ddb.add_response("update_item", {}, claim_params(LINK_APPROVER))
    sfn.add_response("send_task_success", {}, success_params(LINK_APPROVER))
    ddb.add_response("update_item", {}, finalize_params("APPROVED"))

    response = call(http_event("POST", GET_ROUTE, APPROVAL_ID, body=form(confirm=" ABC1 ")))

    assert response["statusCode"] == 200


def test_post_wrong_confirmation_is_400_without_any_write(deps):
    ddb, _ = deps
    stub_get(ddb, record_item())

    response = call(http_event("POST", GET_ROUTE, APPROVAL_ID, body=form(confirm="zzzz")))

    assert response["statusCode"] == 400
    body = html_body(response)
    assert "confirmation code did not match" in body
    assert '<form method="post">' not in body


def test_post_unknown_id_is_404_without_claiming(deps):
    ddb, _ = deps
    stub_get(ddb, None)

    response = call(http_event("POST", GET_ROUTE, APPROVAL_ID, body=form()))

    assert response["statusCode"] == 404


@pytest.mark.parametrize("status", ["APPROVED", "REJECTED", "DECIDING"])
def test_post_on_decided_record_is_409_without_any_write(deps, status):
    ddb, _ = deps
    stub_get(ddb, record_item(status=status))

    response = call(http_event("POST", GET_ROUTE, APPROVAL_ID, body=form()))

    assert response["statusCode"] == 409


def test_post_on_decided_record_is_409_even_with_wrong_confirmation(deps):
    ddb, _ = deps
    stub_get(ddb, record_item(status="APPROVED"))

    response = call(http_event("POST", GET_ROUTE, APPROVAL_ID, body=form(confirm="zzzz")))

    assert response["statusCode"] == 409


def test_second_post_is_409(deps):
    """A concurrent decision wins between our read and our claim: the claim's conditional fails."""
    ddb, _ = deps
    stub_get(ddb, record_item())
    stub_claim_failure(ddb, LINK_APPROVER, record_item(status="APPROVED"))

    response = call(http_event("POST", GET_ROUTE, APPROVAL_ID, body=form()))

    assert response["statusCode"] == 409


def test_decision_on_a_just_expired_record_returns_410(deps):
    """The state machine expired the approval between our read and our claim."""
    ddb, _ = deps
    stub_get(ddb, record_item())
    stub_claim_failure(ddb, LINK_APPROVER, record_item(status="EXPIRED"))

    response = call(http_event("POST", GET_ROUTE, APPROVAL_ID, body=form()))

    # 410, not 409: nobody decided, the window closed. No Step Functions call
    # is stubbed, so resuming the execution would fail the test.
    assert response["statusCode"] == 410
    assert "Approval expired" in html_body(response)


def test_post_on_expired_status_is_410_without_any_write(deps):
    ddb, _ = deps
    stub_get(ddb, record_item(status="EXPIRED"))

    response = call(http_event("POST", GET_ROUTE, APPROVAL_ID, body=form()))

    assert response["statusCode"] == 410


def test_post_after_expiry_is_410(deps):
    ddb, _ = deps
    stub_get(ddb, record_item(expires_at=NOW_ISO))

    response = call(http_event("POST", GET_ROUTE, APPROVAL_ID, body=form()))

    assert response["statusCode"] == 410


def test_post_expiring_between_read_and_claim_is_410(deps):
    ddb, _ = deps
    stub_get(ddb, record_item())
    stub_claim_failure(ddb, LINK_APPROVER, record_item(expires_at=NOW_ISO))

    response = call(http_event("POST", GET_ROUTE, APPROVAL_ID, body=form()))

    assert response["statusCode"] == 410


def test_post_task_timed_out_marks_expired_and_410(deps):
    ddb, sfn = deps
    stub_get(ddb, record_item())
    ddb.add_response("update_item", {}, claim_params(LINK_APPROVER))
    sfn.add_client_error(
        "send_task_success",
        service_error_code="TaskTimedOut",
        http_status_code=400,
        expected_params=success_params(LINK_APPROVER),
    )
    ddb.add_response("update_item", {}, expire_params())

    response = call(http_event("POST", GET_ROUTE, APPROVAL_ID, body=form()))

    assert response["statusCode"] == 410


@pytest.mark.parametrize("code", ["InvalidToken", "TaskDoesNotExist", "ThrottlingException"])
def test_post_refused_callback_reverts_and_500(deps, caplog, code):
    """A ``ClientError`` other than ``TaskTimedOut`` means not delivered, so the claim reverts."""
    ddb, sfn = deps
    stub_get(ddb, record_item())
    ddb.add_response("update_item", {}, claim_params(LINK_APPROVER))
    sfn.add_client_error(
        "send_task_failure",
        service_error_code=code,
        http_status_code=400,
        expected_params=failure_params(LINK_APPROVER),
    )
    ddb.add_response("update_item", {}, revert_params())

    with caplog.at_level(logging.INFO, logger="approval"):
        response = call(http_event("POST", GET_ROUTE, APPROVAL_ID, body=form("reject")))

    assert response["statusCode"] == 500
    html_body(response)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert [r.getMessage() for r in errors] == [
        f"decision callback failed (code={code}), claim reverted"
    ]


def test_unknown_callback_outcome_returns_504_and_keeps_deciding(deps, monkeypatch, caplog):
    """A ``BotoCoreError`` may hide a delivered callback, so the record stays DECIDING."""
    ddb, sfn = deps
    stub_get(ddb, record_item())
    ddb.add_response("update_item", {}, claim_params(LINK_APPROVER))
    calls = []

    def timed_out(**params):
        calls.append(params)
        raise ReadTimeoutError(endpoint_url="https://states.us-east-1.amazonaws.com/")

    # The Stubber cannot raise a BotoCoreError, so the one callback is replaced directly;
    # any revert, expire or finalize would be an unexpected update_item on the ddb Stubber.
    monkeypatch.setattr(sfn.client, "send_task_success", timed_out)

    with caplog.at_level(logging.INFO, logger="approval"):
        response = call(http_event("POST", GET_ROUTE, APPROVAL_ID, body=form()))

    # 504, not 500: "not recorded, try again" would be a lie here.
    assert response["statusCode"] == 504
    page = html_body(response)
    assert "Decision status unknown" in page
    assert "The decision may have been recorded." in page
    assert calls == [success_params(LINK_APPROVER)]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert [r.getMessage() for r in errors] == [
        "decision callback outcome unknown (ReadTimeoutError), approval left DECIDING"
    ]


def test_unknown_callback_outcome_on_the_iam_route_is_504_json(deps, monkeypatch):
    ddb, sfn = deps
    stub_get(ddb, record_item())
    ddb.add_response("update_item", {}, claim_params(IAM_APPROVER))

    def timed_out(**_params):
        raise ReadTimeoutError(endpoint_url="https://states.us-east-1.amazonaws.com/")

    monkeypatch.setattr(sfn.client, "send_task_success", timed_out)

    response = call(
        http_event(
            "POST",
            IAM_ROUTE,
            APPROVAL_ID,
            body=json.dumps({"decision": "approve"}),
            iam_arn=SSO_CALLER_ARN,
        )
    )

    assert response["statusCode"] == 504
    assert json_body(response) == {"error": "unknown outcome"}


def test_finalize_read_timeout_still_returns_200(deps, monkeypatch, caplog):
    """The callback was delivered; a network error while recording it changes nothing."""
    ddb, sfn = deps
    stub_get(ddb, record_item())
    ddb.add_response("update_item", {}, claim_params(LINK_APPROVER))
    sfn.add_response("send_task_success", {}, success_params(LINK_APPROVER))
    stubbed_update = ddb.client.update_item
    updates = []

    def claim_then_time_out(**params):
        updates.append(params["UpdateExpression"])
        if len(updates) == 1:
            return stubbed_update(**params)
        raise ReadTimeoutError(endpoint_url="https://dynamodb.us-east-1.amazonaws.com/")

    # The Stubber cannot raise a BotoCoreError: the claim goes to the stub, finalize times out.
    monkeypatch.setattr(ddb.client, "update_item", claim_then_time_out)

    with caplog.at_level(logging.INFO, logger="approval"):
        response = call(http_event("POST", GET_ROUTE, APPROVAL_ID, body=form()))

    assert response["statusCode"] == 200
    assert "Plan approved" in html_body(response)
    assert updates == [
        "SET #s = :deciding, approvedBy = :by",
        *["SET #s = :status, decidedAt = :at"] * 3,
    ]
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == [
        "decision delivered but the approval was not finalized after 3 attempts (ReadTimeoutError)"
    ]
    assert APPROVAL_ID not in caplog.text


def test_finalize_is_retried_until_the_record_reads_approved(deps, caplog):
    """The executor applies only an APPROVED record, so one failed write must not strand it."""
    ddb, sfn = deps
    stub_get(ddb, record_item())
    ddb.add_response("update_item", {}, claim_params(LINK_APPROVER))
    sfn.add_response("send_task_success", {}, success_params(LINK_APPROVER))
    ddb.add_client_error(
        "update_item",
        service_error_code="ProvisionedThroughputExceededException",
        http_status_code=400,
        expected_params=finalize_params("APPROVED"),
    )
    ddb.add_response("update_item", {}, finalize_params("APPROVED"))

    with caplog.at_level(logging.INFO, logger="approval"):
        response = call(http_event("POST", GET_ROUTE, APPROVAL_ID, body=form()))

    assert response["statusCode"] == 200
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR] == []
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        "finalize attempt 1 of 3 failed (code=ProvisionedThroughputExceededException), retrying"
    ]


def test_post_finalize_store_error_after_delivered_decision_is_still_200(deps, caplog):
    ddb, sfn = deps
    stub_get(ddb, record_item())
    ddb.add_response("update_item", {}, claim_params(LINK_APPROVER))
    sfn.add_response("send_task_success", {}, success_params(LINK_APPROVER))
    for _attempt in range(3):
        ddb.add_client_error(
            "update_item",
            service_error_code="ProvisionedThroughputExceededException",
            http_status_code=400,
            expected_params=finalize_params("APPROVED"),
        )

    with caplog.at_level(logging.INFO, logger="approval"):
        response = call(http_event("POST", GET_ROUTE, APPROVAL_ID, body=form()))

    assert response["statusCode"] == 200
    assert "Plan approved" in html_body(response)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert [r.getMessage() for r in errors] == [
        "decision delivered but the approval was not finalized after 3 attempts "
        "(code=ProvisionedThroughputExceededException)"
    ]


def test_post_finalize_conflict_after_delivered_decision_is_still_200(deps):
    ddb, sfn = deps
    stub_get(ddb, record_item())
    ddb.add_response("update_item", {}, claim_params(LINK_APPROVER))
    sfn.add_response("send_task_success", {}, success_params(LINK_APPROVER))
    ddb.add_client_error(
        "update_item",
        service_error_code="ConditionalCheckFailedException",
        http_status_code=400,
        expected_params=finalize_params("APPROVED"),
    )

    response = call(http_event("POST", GET_ROUTE, APPROVAL_ID, body=form()))

    assert response["statusCode"] == 200


def test_unexpected_store_error_is_500_page_with_headers(deps):
    ddb, _ = deps
    ddb.add_client_error(
        "get_item",
        service_error_code="InternalServerError",
        http_status_code=500,
        expected_params=get_params(),
    )

    response = call(http_event("GET", GET_ROUTE, APPROVAL_ID))

    assert response["statusCode"] == 500
    assert APPROVAL_ID not in html_body(response)
    for name, value in HARDENING_HEADERS.items():
        assert response["headers"][name] == value


# POST /iam/approvals/{id}


@pytest.mark.parametrize(
    "caller",
    [
        None,
        f"arn:aws:sts::{ACCOUNT}:assumed-role/SomeOtherRole/user",
        f"arn:aws:sts::{ACCOUNT}:assumed-role/AWSReservedSSO_Admin_abcd/user",
        f"arn:aws:iam::{ACCOUNT}:user/AWSReservedSSO_Admin_abc",
        "arn:aws:sts::111122223333:assumed-role/AWSReservedSSO_Admin_abc/user",
        f"arn:aws:sts::{ACCOUNT}:federated-user/AWSReservedSSO_Admin_abc",
    ],
)
def test_iam_route_rejects_other_principals_before_any_write(deps, caller):
    event = http_event(
        "POST", IAM_ROUTE, APPROVAL_ID, body=json.dumps({"decision": "approve"}), iam_arn=caller
    )

    response = call(event)

    assert response["statusCode"] == 403
    assert json_body(response) == {"error": "forbidden"}


def test_iam_route_rejects_everyone_when_no_approver_role_is_set(deps, monkeypatch):
    monkeypatch.setenv("APPROVER_ROLE_ARN", "")
    event = http_event(
        "POST",
        IAM_ROUTE,
        APPROVAL_ID,
        body=json.dumps({"decision": "approve"}),
        iam_arn=SSO_CALLER_ARN,
    )

    assert call(event)["statusCode"] == 403


def test_iam_route_accepts_sso_permission_set_role(deps):
    ddb, sfn = deps
    stub_get(ddb, record_item())
    ddb.add_response("update_item", {}, claim_params(IAM_APPROVER))
    sfn.add_response("send_task_success", {}, success_params(IAM_APPROVER))
    ddb.add_response("update_item", {}, finalize_params("APPROVED"))
    event = http_event(
        "POST",
        IAM_ROUTE,
        APPROVAL_ID,
        body=json.dumps({"decision": "approve"}),
        iam_arn=SSO_CALLER_ARN,
        base64_body=False,
    )

    response = call(event)

    assert response["statusCode"] == 200
    assert json_body(response) == {"status": "APPROVED"}


def test_iam_route_reject_with_exact_role_arn(deps, monkeypatch):
    ddb, sfn = deps
    role_arn = f"arn:aws:iam::{ACCOUNT}:user/approver"
    monkeypatch.setenv("APPROVER_ROLE_ARN", role_arn)
    approver = {"mode": "iam", "principal": role_arn, "at": NOW_ISO}
    stub_get(ddb, record_item())
    ddb.add_response("update_item", {}, claim_params(approver))
    sfn.add_response("send_task_failure", {}, failure_params(approver))
    ddb.add_response("update_item", {}, finalize_params("REJECTED"))
    event = http_event(
        "POST", IAM_ROUTE, APPROVAL_ID, body=json.dumps({"decision": "reject"}), iam_arn=role_arn
    )

    response = call(event)

    assert response["statusCode"] == 200
    assert json_body(response) == {"status": "REJECTED"}


@pytest.mark.parametrize(
    "body", [None, "", "not json", "[]", '{"decision": "maybe"}', '{"decision": 1}']
)
def test_iam_route_bad_body_is_400_and_writes_nothing(deps, body):
    event = http_event("POST", IAM_ROUTE, APPROVAL_ID, body=body, iam_arn=SSO_CALLER_ARN)

    response = call(event)

    assert response["statusCode"] == 400
    assert "error" in json_body(response)


def test_iam_route_second_post_is_409_json(deps):
    ddb, _ = deps
    stub_get(ddb, record_item())
    stub_claim_failure(ddb, IAM_APPROVER, record_item(status="REJECTED"))
    event = http_event(
        "POST",
        IAM_ROUTE,
        APPROVAL_ID,
        body=json.dumps({"decision": "approve"}),
        iam_arn=SSO_CALLER_ARN,
    )

    response = call(event)

    assert response["statusCode"] == 409
    assert "error" in json_body(response)


def test_iam_route_unknown_id_is_404_json(deps):
    ddb, _ = deps
    stub_get(ddb, None)
    event = http_event(
        "POST",
        IAM_ROUTE,
        APPROVAL_ID,
        body=json.dumps({"decision": "approve"}),
        iam_arn=SSO_CALLER_ARN,
    )

    response = call(event)

    assert response["statusCode"] == 404
    assert "error" in json_body(response)


# Route gating


def test_iam_route_is_404_when_auth_mode_is_link(deps, monkeypatch):
    monkeypatch.setenv("APPROVAL_AUTH", "link")
    event = http_event(
        "POST",
        IAM_ROUTE,
        APPROVAL_ID,
        body=json.dumps({"decision": "approve"}),
        iam_arn=SSO_CALLER_ARN,
    )

    response = call(event)

    assert response["statusCode"] == 404


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_link_routes_are_404_when_auth_mode_is_iam(deps, monkeypatch, method):
    monkeypatch.setenv("APPROVAL_AUTH", "iam")
    body = form() if method == "POST" else None

    response = call(http_event(method, GET_ROUTE, APPROVAL_ID, body=body))

    assert response["statusCode"] == 404


@pytest.mark.parametrize("mode", [None, "", "everyone"])
def test_every_route_is_404_when_auth_mode_is_unknown(deps, monkeypatch, mode):
    if mode is None:
        monkeypatch.delenv("APPROVAL_AUTH")
    else:
        monkeypatch.setenv("APPROVAL_AUTH", mode)

    assert call(http_event("GET", GET_ROUTE, APPROVAL_ID))["statusCode"] == 404
    assert call(http_event("POST", GET_ROUTE, APPROVAL_ID, body=form()))["statusCode"] == 404


def test_unknown_route_is_404(deps):
    response = call(http_event("DELETE", GET_ROUTE, APPROVAL_ID))

    assert response["statusCode"] == 404


def test_missing_path_id_is_404(deps):
    response = call(http_event("GET", GET_ROUTE, None))

    assert response["statusCode"] == 404


# Cross-cutting


def test_responses_carry_hardening_headers(deps):
    ddb, _ = deps
    stub_get(ddb, record_item())
    responses = [
        call(http_event("GET", GET_ROUTE, APPROVAL_ID)),
        call(http_event("POST", GET_ROUTE, APPROVAL_ID, body="")),
        call(http_event("POST", IAM_ROUTE, APPROVAL_ID, body="{}", iam_arn="nobody")),
        call(http_event("PUT", "/nowhere", None)),
    ]

    for response in responses:
        for name, value in HARDENING_HEADERS.items():
            assert response["headers"][name] == value
    assert responses[0]["headers"]["Content-Type"] == "text/html; charset=utf-8"
    assert responses[2]["headers"]["Content-Type"] == "application/json"


def test_logs_never_contain_the_approval_id(deps, caplog):
    ddb, sfn = deps
    stub_get(ddb, record_item())
    stub_get(ddb, record_item())
    ddb.add_response("update_item", {}, claim_params(LINK_APPROVER))
    sfn.add_client_error(
        "send_task_success",
        service_error_code="InvalidToken",
        http_status_code=400,
        expected_params=success_params(LINK_APPROVER),
    )
    ddb.add_response("update_item", {}, revert_params())
    stub_get(ddb, record_item())
    ddb.add_response("update_item", {}, claim_params(LINK_APPROVER))
    sfn.add_response("send_task_success", {}, success_params(LINK_APPROVER))
    ddb.add_response("update_item", {}, finalize_params("APPROVED"))

    with caplog.at_level(logging.DEBUG):
        call(http_event("POST", GET_ROUTE, APPROVAL_ID, body=form(confirm="zzzz")))
        call(http_event("POST", GET_ROUTE, APPROVAL_ID, body=form()))
        call(http_event("POST", GET_ROUTE, APPROVAL_ID, body=form()))
        call(http_event("POST", IAM_ROUTE, APPROVAL_ID, body="{}", iam_arn="nobody"))

    approval_records = [r for r in caplog.records if r.name == "approval"]
    assert approval_records
    for record in caplog.records:
        dumped = repr(record.__dict__) + record.getMessage()
        assert APPROVAL_ID not in dumped
        assert TASK_TOKEN not in dumped
        assert "confirm=" not in dumped
    for record in approval_records:
        assert record.approvalIdHash == short_hash(APPROVAL_ID)


def test_unexpected_failure_logs_no_traceback_and_no_secret(deps, monkeypatch, caplog):
    """A chained store error whose message echoes the id and token never reaches the logs."""
    ddb, _ = deps
    stub_get(ddb, record_item())
    ddb.add_client_error(
        "update_item",
        service_error_code="ConditionalCheckFailedException",
        service_message=f"condition failed for {APPROVAL_ID} with {TASK_TOKEN}",
        http_status_code=400,
        expected_params=finalize_params("APPROVED"),
    )

    def failing_decide(store, states, found, request, log):
        # The real store raises AlreadyDecidedError chained from the ClientError above.
        store.finalize(request.approval_id, "APPROVED", request.now)
        raise AssertionError("finalize should have raised")

    monkeypatch.setattr(approval, "decide", failing_decide)

    with caplog.at_level(logging.DEBUG):
        response = call(http_event("POST", GET_ROUTE, APPROVAL_ID, body=form()))

    assert response["statusCode"] == 500
    assert APPROVAL_ID not in html_body(response)
    failures = [r for r in caplog.records if r.getMessage().startswith("approval request failed")]
    assert len(failures) == 1
    assert failures[0].levelno == logging.ERROR
    assert not failures[0].exc_info
    assert failures[0].getMessage() == (
        f"approval request failed (AlreadyDecidedError: {short_hash(APPROVAL_ID)})"
    )
    formatter = logging.Formatter()
    for record in caplog.records:
        dumped = repr(record.__dict__) + record.getMessage() + (record.exc_text or "")
        if record.exc_info:
            dumped += formatter.formatException(record.exc_info)
        assert APPROVAL_ID not in dumped
        assert TASK_TOKEN not in dumped
    assert APPROVAL_ID not in caplog.text
    assert TASK_TOKEN not in caplog.text


# allowed_principal


@pytest.mark.parametrize(
    ("principal", "role_arn", "allowed"),
    [
        (SSO_CALLER_ARN, SSO_ROLE_ARN, True),
        (
            f"arn:aws:sts::{ACCOUNT}:assumed-role/Approver/session",
            f"arn:aws:iam::{ACCOUNT}:role/Approver",
            True,
        ),
        (
            f"arn:aws:sts::{ACCOUNT}:assumed-role/Approver/session",
            f"arn:aws:iam::{ACCOUNT}:role/team/Approver",
            True,
        ),
        (f"arn:aws:iam::{ACCOUNT}:user/me", f"arn:aws:iam::{ACCOUNT}:user/me", True),
        (
            f"arn:aws:sts::{ACCOUNT}:assumed-role/Approver/session",
            f"arn:aws:iam::{ACCOUNT}:role/Other",
            False,
        ),
        (
            "arn:aws:sts::111122223333:assumed-role/Approver/session",
            f"arn:aws:iam::{ACCOUNT}:role/Approver",
            False,
        ),
        (
            f"arn:aws:sts::{ACCOUNT}:assumed-role/Approver",
            f"arn:aws:iam::{ACCOUNT}:role/Approver",
            False,
        ),
        (
            f"arn:aws:iam::{ACCOUNT}:user/Approver",
            f"arn:aws:iam::{ACCOUNT}:role/Approver",
            False,
        ),
        (f"arn:aws:sts::{ACCOUNT}:assumed-role/Approver/session", "", False),
        ("", "", False),
        ("", f"arn:aws:iam::{ACCOUNT}:role/Approver", False),
    ],
)
def test_allowed_principal(principal, role_arn, allowed):
    assert approval.allowed_principal(principal, role_arn) is allowed


@pytest.mark.parametrize(
    "confirm",
    [
        "12\ufb00",  # the ligature casefolds to "ff"
        "\u00a012ff",  # no-break space
        "12ff\u3000",  # ideographic space
        "\uff11\uff12ff",  # fullwidth digits
        "12f",
        "12fff",
        "",
    ],
    ids=ascii,
)
def test_confirmation_code_is_compared_as_ascii(confirm):
    assert approval._confirmation_matches(confirm, "20261102T120000Z-12ff") is False


@pytest.mark.parametrize("confirm", ["12ff", "12FF", " 12ff\t", "12Ff\r\n"])
def test_confirmation_code_ignores_ascii_case_and_surrounding_blanks(confirm):
    assert approval._confirmation_matches(confirm, "20261102T120000Z-12ff") is True
