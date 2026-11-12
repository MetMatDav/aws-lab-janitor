"""Surface 4: leaking the task token or the approval id.

The task token resumes the execution and the approval id is a bearer credential
while the approval is pending. Neither may show up in a log line, an exception
message (Lambda logs it and Step Functions forwards it to the alerts email), an
HTTP response, or an email other than the approval request and its reminder.
"""

import io
import json
import logging
import traceback

import pytest
from botocore.response import StreamingBody

from notifier import approval as notifier_approval
from notifier import handler as notifier
from notifier import outcomes, reminder
from shared.approvals import (
    AlreadyDecidedError,
    ApprovalExistsError,
    ApprovalStore,
    ExpiredError,
    short_hash,
)
from shared.plan import plan_bytes, plan_sha256
from shared.plan_store import PlanStore
from tests.adversarial.approval_helpers import (
    CALLER_ARN,
    IAM_APPROVER,
    IAM_ROUTE,
    LINK_APPROVER,
    LINK_ROUTE,
    approval_record,
    call,
    claim_params,
    failure_params,
    finalize_params,
    form,
    get_params,
    http_event,
    record_item,
    success_params,
    summary,
)
from tests.adversarial.helpers import APPROVAL_ID, RUN_ID, TASK_TOKEN

BUCKET = "test-plans"
TABLE = "test-audit"
APPROVALS_ARN = "arn:aws:sns:us-east-1:123456789012:approvals"
REPORTS_ARN = "arn:aws:sns:us-east-1:123456789012:reports"
URL = "https://abc123.execute-api.us-east-1.amazonaws.com"
SECRETS = (APPROVAL_ID, TASK_TOKEN)

PLAN = {
    "runId": RUN_ID,
    "generatedAt": "2026-11-02T12:00:00+00:00",
    "candidates": [
        {
            "region": "us-east-1",
            "kind": "ebs-volume",
            "resourceId": "vol-1",
            "reason": "tag Purpose=lab, available, 42 days old",
            "estimatedMonthlyUsd": 0.8,
        }
    ],
    "errors": [],
}


def approval_event() -> dict:
    return {
        "mode": "approval",
        "taskToken": TASK_TOKEN,
        "approvalId": APPROVAL_ID,
        "detect": {
            "runId": RUN_ID,
            "planVersionId": "plan-v1",
            "planSha256": plan_sha256(plan_bytes(PLAN)),
            "reportKey": f"plans/{RUN_ID}/report.txt",
            "reportVersionId": "report-v1",
            "candidateCount": 1,
            "estimatedMonthlyUsd": 0.8,
            "dryRun": False,
        },
        "config": {"dryRun": False, "minAgeDays": 7},
    }


def s3_body(data: bytes) -> dict:
    return {"Body": StreamingBody(io.BytesIO(data), len(data))}


def subscribed(sns, topic_arn: str, arn: str | None = None) -> None:
    sns.add_response(
        "list_subscriptions_by_topic",
        {
            "Subscriptions": [
                {
                    "SubscriptionArn": arn or f"{topic_arn}:11111111-2222-3333-4444-555555555555",
                    "Owner": "123456789012",
                    "Protocol": "email",
                    "Endpoint": "owner@example.com",
                    "TopicArn": topic_arn,
                }
            ]
        },
    )


def _janitor_logs(caplog) -> str:
    """Everything the janitor's own loggers recorded (botocore's DEBUG output is not ours)."""
    own = [entry for entry in caplog.records if not entry.name.startswith(("botocore", "boto3"))]
    return "".join(entry.getMessage() + repr(vars(entry)) for entry in own)


@pytest.fixture
def notifier_stubs(monkeypatch, s3_stub, sns_stub, ddb_stub):
    monkeypatch.setenv("APPROVALS_TOPIC_ARN", APPROVALS_ARN)
    monkeypatch.setenv("APPROVAL_URL", URL)
    monkeypatch.setenv("APPROVAL_AUTH", "both")
    monkeypatch.setattr(
        notifier,
        "build_dependencies",
        lambda: (PlanStore(s3_stub[0], BUCKET), sns_stub[0], ApprovalStore(ddb_stub[0], TABLE)),
    )
    return s3_stub[1], sns_stub[1], ddb_stub[1]


def _fail_no_subscriber(s3, sns, ddb):
    subscribed(sns, APPROVALS_ARN, "PendingConfirmation")


def _fail_plan_read(s3, sns, ddb):
    subscribed(sns, APPROVALS_ARN)
    subscribed(sns, REPORTS_ARN)
    s3.add_client_error("get_object", service_error_code="AccessDenied", http_status_code=403)


def _fail_sha(s3, sns, ddb):
    subscribed(sns, APPROVALS_ARN)
    subscribed(sns, REPORTS_ARN)
    s3.add_response("get_object", s3_body(plan_bytes(PLAN) + b" "))


def _ready(s3, sns):
    subscribed(sns, APPROVALS_ARN)
    subscribed(sns, REPORTS_ARN)
    s3.add_response("get_object", s3_body(plan_bytes(PLAN)))


def _fail_record_write(s3, sns, ddb):
    _ready(s3, sns)
    ddb.add_client_error("put_item", service_error_code="ProvisionedThroughputExceededException")


def _fail_record_oversize(s3, sns, ddb):
    _ready(s3, sns)
    ddb.add_client_error(
        "put_item",
        service_error_code="ValidationException",
        service_message="Item size has exceeded the maximum allowed size",
    )


def _fail_refresh(s3, sns, ddb):
    _ready(s3, sns)
    ddb.add_client_error("put_item", service_error_code="ConditionalCheckFailedException")
    ddb.add_client_error("update_item", service_error_code="ConditionalCheckFailedException")


def _fail_publish(s3, sns, ddb):
    _ready(s3, sns)
    ddb.add_response("put_item", {})
    sns.add_client_error(
        "publish",
        service_error_code="InvalidParameter",
        service_message="Invalid parameter: Message too long",
    )


def _fail_report(s3, sns, ddb):
    _ready(s3, sns)
    ddb.add_response("put_item", {})
    sns.add_response("publish", {"MessageId": "m-1"})
    s3.add_client_error("get_object", service_error_code="NoSuchVersion", http_status_code=404)


@pytest.mark.parametrize(
    "arrange",
    [
        _fail_no_subscriber,
        _fail_plan_read,
        _fail_sha,
        _fail_record_write,
        _fail_record_oversize,
        _fail_refresh,
        _fail_publish,
        _fail_report,
    ],
    ids=lambda function: function.__name__.removeprefix("_fail_"),
)
def test_no_failure_of_the_approval_request_carries_the_token_or_the_id(
    notifier_stubs, caplog, arrange
):
    """Whatever breaks, the error Lambda logs and Step Functions emails holds no secret.

    The whole chained traceback is checked: that is what the Lambda runtime prints and
    what lands in the ``Cause`` that ``NotifyFailure`` sends to the alerts topic.
    """
    arrange(*notifier_stubs)

    with caplog.at_level(logging.DEBUG), pytest.raises(Exception) as raised:
        notifier.handler(approval_event(), None)

    printed = "".join(traceback.format_exception(raised.value)) + repr(raised.value)
    logged = _janitor_logs(caplog)
    for secret in SECRETS:
        assert secret not in printed
        assert secret not in logged


def test_successful_approval_request_logs_only_the_hash_and_returns_no_token(
    notifier_stubs, caplog
):
    s3, sns, ddb = notifier_stubs
    _ready(s3, sns)
    ddb.add_response("put_item", {})
    sns.add_response("publish", {"MessageId": "m-1"})
    s3.add_response("get_object", s3_body(b"WEEKLY REPORT"))
    sns.add_response("publish", {"MessageId": "m-2"})

    with caplog.at_level(logging.DEBUG):
        result = notifier.handler(approval_event(), None)

    logged = _janitor_logs(caplog)
    assert TASK_TOKEN not in json.dumps(result)
    assert TASK_TOKEN not in logged
    assert APPROVAL_ID not in logged
    assert short_hash(APPROVAL_ID) in logged


@pytest.mark.parametrize(("auth", "times"), [("link", 1), ("iam", 2), ("both", 3)])
def test_approval_and_reminder_emails_carry_the_id_only_in_the_decision_lines(auth, times):
    rec = approval_record()
    bodies = [
        notifier_approval.approval_message(rec, summary(), URL, auth, RUN_ID[-4:]),
        reminder.reminder_message(rec, URL, auth, RUN_ID[-4:]),
    ]

    for body in bodies:
        assert body.count(APPROVAL_ID) == times
        with_id = [line for line in body.split("\n") if APPROVAL_ID in line]
        assert all(
            line.startswith(("Review and decide: ", "python scripts/approve.py --url "))
            for line in with_id
        )
        assert TASK_TOKEN not in body
        assert "taskToken" not in body


def test_outcome_emails_never_repeat_the_approval_id(monkeypatch):
    """The rejection cause and the executor result both carry the id; the emails must not."""
    sent = []
    monkeypatch.setattr(
        outcomes, "_publish", lambda subject, message, run_id: sent.append(subject + message)
    )
    cause = json.dumps({"approvalId": APPROVAL_ID, "rejectedBy": LINK_APPROVER})
    detect = {"runId": RUN_ID}
    apply = {
        "runId": RUN_ID,
        "approvalId": APPROVAL_ID,
        "deleted": 1,
        "failed": 0,
        "skipped": 0,
        "items": [
            {
                "resourceId": "vol-1",
                "kind": "ebs-volume",
                "region": "us-east-1",
                "outcome": "DELETED",
                "reason": "planned",
            }
        ],
    }
    approval = {"decision": "approved", "approvalId": APPROVAL_ID, "approvedBy": LINK_APPROVER}

    outcomes.publish_rejected({"detect": detect, "error": {"Error": "Rejected", "Cause": cause}})
    outcomes.publish_rejected({"detect": detect, "error": {"Cause": json.dumps(APPROVAL_ID)}})
    outcomes.publish_expired({"detect": detect, "error": {"Cause": cause}})
    outcomes.publish_result({"detect": detect, "apply": apply, "approval": approval})

    assert len(sent) == 4
    for message in sent:
        assert APPROVAL_ID not in message
        assert TASK_TOKEN not in message


def test_approval_store_errors_name_the_hash_never_the_id(ddb_stub):
    client, stubber = ddb_stub
    store = ApprovalStore(client, TABLE)
    conditional = "ConditionalCheckFailedException"
    stubber.add_client_error(
        "update_item", service_error_code=conditional, modeled_fields={"Item": record_item()}
    )
    stubber.add_client_error(
        "update_item",
        service_error_code=conditional,
        modeled_fields={"Item": record_item("APPROVED")},
    )
    stubber.add_client_error("update_item", service_error_code=conditional)
    stubber.add_client_error("update_item", service_error_code=conditional)

    errors = []
    for attempt in (
        lambda: store.claim(APPROVAL_ID, "2099-01-01T00:00:00+00:00", {"mode": "link"}),
        lambda: store.claim(APPROVAL_ID, "2026-11-03T09:00:00+00:00", {"mode": "link"}),
        lambda: store.finalize(APPROVAL_ID, "APPROVED", "2026-11-03T09:00:00+00:00"),
        lambda: store.refresh_token(APPROVAL_ID, TASK_TOKEN),
    ):
        with pytest.raises((ExpiredError, AlreadyDecidedError, ApprovalExistsError)) as raised:
            attempt()
        errors.append(raised.value)

    assert [type(error) for error in errors] == [
        ExpiredError,
        AlreadyDecidedError,
        AlreadyDecidedError,
        AlreadyDecidedError,
    ]
    for error in errors:
        printed = "".join(traceback.format_exception(error))
        assert short_hash(APPROVAL_ID) in str(error)
        assert APPROVAL_ID not in printed
        assert TASK_TOKEN not in printed


class _RecordingTable:
    """A DynamoDB client stand-in that keeps the key of every call it receives."""

    def __init__(self) -> None:
        self.keys: list[dict] = []

    def put_item(self, **params) -> dict:
        self.keys.append({name: params["Item"][name] for name in ("pk", "sk")})
        return {}

    def get_item(self, **params) -> dict:
        self.keys.append(params["Key"])
        return {}

    def update_item(self, **params) -> dict:
        self.keys.append(params["Key"])
        return {"Attributes": record_item()}


@pytest.mark.xfail(
    strict=True,
    reason="the approval record's partition key is the bearer id itself: whoever can read the "
    "audit table or the CloudTrail data events the stack records for it (item keys are "
    "logged in clear) can open the link and approve; docs/runbook.md accepts this today",
)
def test_no_dynamodb_key_carries_the_approval_id_in_clear():
    """Foothold: read-only access to the audit table or to the trail's log bucket, in the
    ``link`` and ``both`` modes (in ``iam`` mode the id alone decides nothing).

    The handler keeps the id out of its own logs and out of the API access log, but every
    GetItem, PutItem and UpdateItem on the record names ``APPROVAL#<id>`` as its key, and
    the stack's own trail selects the table's data events. Keying the record by a digest
    of the id would leave nothing replayable in either place.
    """
    table = _RecordingTable()
    store = ApprovalStore(table, TABLE)

    store.create(approval_record(), TASK_TOKEN)
    store.get(APPROVAL_ID)
    store.refresh_token(APPROVAL_ID, TASK_TOKEN)
    store.mark_reminded(APPROVAL_ID, "2026-11-03T09:00:00+00:00")
    store.claim(APPROVAL_ID, "2026-11-03T09:00:00+00:00", {"mode": "link"})
    store.finalize(APPROVAL_ID, "APPROVED", "2026-11-03T09:00:00+00:00")

    assert len(table.keys) == 6
    assert APPROVAL_ID not in json.dumps(table.keys)


# --- the approval endpoint -------------------------------------------------------------------


def _get_200(api):
    api.ddb.add_response("get_item", {"Item": record_item()}, get_params())
    return http_event("GET", LINK_ROUTE)


def _get_decided(api):
    api.ddb.add_response("get_item", {"Item": record_item("APPROVED")}, get_params())
    return http_event("GET", LINK_ROUTE)


def _post_approve(api):
    api.ddb.add_response("get_item", {"Item": record_item()}, get_params())
    api.ddb.add_response("update_item", {}, claim_params(LINK_APPROVER))
    api.sfn.add_response("send_task_success", {}, success_params(LINK_APPROVER))
    api.ddb.add_response("update_item", {}, finalize_params("APPROVED"))
    return http_event("POST", LINK_ROUTE, body=form())


def _post_reject(api):
    api.ddb.add_response("get_item", {"Item": record_item()}, get_params())
    api.ddb.add_response("update_item", {}, claim_params(LINK_APPROVER))
    api.sfn.add_response("send_task_failure", {}, failure_params(LINK_APPROVER))
    api.ddb.add_response("update_item", {}, finalize_params("REJECTED"))
    return http_event("POST", LINK_ROUTE, body=form(decision="reject"))


def _post_wrong_code(api):
    api.ddb.add_response("get_item", {"Item": record_item()}, get_params())
    return http_event("POST", LINK_ROUTE, body=form(confirm="zzzz"))


def _iam_approve(api):
    api.ddb.add_response("get_item", {"Item": record_item()}, get_params())
    api.ddb.add_response("update_item", {}, claim_params(IAM_APPROVER))
    api.sfn.add_response("send_task_success", {}, success_params(IAM_APPROVER))
    api.ddb.add_response("update_item", {}, finalize_params("APPROVED"))
    return http_event("POST", IAM_ROUTE, body='{"decision": "approve"}', iam_arn=CALLER_ARN)


def _callback_refused_with_an_echo(api):
    api.ddb.add_response("get_item", {"Item": record_item()}, get_params())
    api.ddb.add_response("update_item", {}, claim_params(LINK_APPROVER))
    api.sfn.add_client_error(
        "send_task_success",
        service_error_code="InvalidToken",
        service_message=f"Invalid Token: '{TASK_TOKEN}' for {APPROVAL_ID}",
    )
    api.ddb.add_response("update_item", {})
    return http_event("POST", LINK_ROUTE, body=form())


def _finalize_refused_with_an_echo(api):
    api.ddb.add_response("get_item", {"Item": record_item()}, get_params())
    api.ddb.add_response("update_item", {}, claim_params(LINK_APPROVER))
    api.sfn.add_response("send_task_success", {}, success_params(LINK_APPROVER))
    for _ in range(3):
        api.ddb.add_client_error(
            "update_item",
            service_error_code="ValidationException",
            service_message=f"bad update for APPROVAL#{APPROVAL_ID} token {TASK_TOKEN}",
        )
    return http_event("POST", LINK_ROUTE, body=form())


@pytest.mark.parametrize(
    "arrange",
    [
        _get_200,
        _get_decided,
        _post_approve,
        _post_reject,
        _post_wrong_code,
        _iam_approve,
        _callback_refused_with_an_echo,
        _finalize_refused_with_an_echo,
    ],
    ids=lambda function: function.__name__.strip("_"),
)
def test_no_response_or_log_line_of_the_endpoint_carries_the_id_or_the_token(api, caplog, arrange):
    """Every outcome, including AWS errors whose message echoes both values back."""
    event = arrange(api)

    with (
        caplog.at_level(logging.DEBUG, logger="approval"),
        caplog.at_level(logging.DEBUG, logger="shared.approvals"),
    ):
        response = call(event)

    own = [entry for entry in caplog.records if entry.name in {"approval", "shared.approvals"}]
    logged = "".join(entry.getMessage() + repr(vars(entry)) for entry in own)
    answered = json.dumps(response)
    for secret in (*SECRETS, "taskToken"):
        assert secret not in answered
        assert secret not in logged


@pytest.mark.parametrize("operation", ["get_item", "update_item"])
def test_store_error_that_echoes_the_key_does_not_reach_the_log(api, caplog, operation):
    """Foothold: read access to the function's logs, and an error text that quotes the key.

    No DynamoDB message known to do so was found; ``decision.py`` already logs only codes
    for Step Functions errors for this very reason, the catch-all in ``handler.py`` does not.
    """
    echo = f"The provided key APPROVAL#{APPROVAL_ID} is not valid"
    if operation == "update_item":
        api.ddb.add_response("get_item", {"Item": record_item()}, get_params())
    api.ddb.add_client_error(
        operation, service_error_code="ValidationException", service_message=echo
    )

    with caplog.at_level(logging.INFO):
        response = call(http_event("POST", LINK_ROUTE, body=form()))

    # Only the janitor's own loggers: botocore's DEBUG output is not what Lambda records.
    own = [entry.getMessage() for entry in caplog.records if entry.name == "approval"]
    assert response["statusCode"] == 500
    assert APPROVAL_ID not in response["body"]
    assert own
    assert APPROVAL_ID not in "".join(own)
