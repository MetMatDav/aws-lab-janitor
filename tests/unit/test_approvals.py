import logging
from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from shared.approvals import (
    APPROVAL_WINDOW,
    AlreadyDecidedError,
    ApprovalError,
    ApprovalExistsError,
    ApprovalRecord,
    ApprovalStore,
    ExpiredError,
    expiry_for,
    short_hash,
)

TABLE = "audit"
APPROVAL_ID = "a1b2"
# Error messages carry the hash, never the id (the id grants access to the approval).
ID_HASH = short_hash(APPROVAL_ID)
PK = {"S": "APPROVAL#a1b2"}
SK = {"S": "META"}
KEY = {"pk": PK, "sk": SK}
NOW = "2026-11-03T13:00:00+00:00"
APPROVER = {"sub": "user-1", "email": "approver@example.com"}
APPROVER_ATTR = {"M": {"sub": {"S": "user-1"}, "email": {"S": "approver@example.com"}}}

RECORD = ApprovalRecord(
    approval_id=APPROVAL_ID,
    run_id="20261103T120000Z-7f3a",
    plan_version_id="v1",
    plan_sha256="ab" * 32,
    plan_summary={"candidateCount": 1},
    status="PENDING",
    created_at="2026-11-03T12:00:00+00:00",
    expires_at="2026-11-06T11:30:00+00:00",
    approved_by=None,
    decided_at=None,
)
TTL = int(datetime(2026, 11, 6, 11, 30, tzinfo=UTC).timestamp()) + 30 * 24 * 3600

RECORD_ITEM = {
    "pk": PK,
    "sk": SK,
    "approvalId": {"S": APPROVAL_ID},
    "runId": {"S": RECORD.run_id},
    "planVersionId": {"S": "v1"},
    "planSha256": {"S": "ab" * 32},
    "planSummary": {"M": {"candidateCount": {"N": "1"}}},
    "taskToken": {"S": "tok"},
    "status": {"S": "PENDING"},
    "createdAt": {"S": RECORD.created_at},
    "expiresAt": {"S": RECORD.expires_at},
    "ttl": {"N": str(TTL)},
}


@pytest.fixture
def ddb_stub(stub_factory) -> Stubber:
    return stub_factory("dynamodb")


def store(stub: Stubber) -> ApprovalStore:
    return ApprovalStore(stub.client, TABLE)


def conditional_failure(stub: Stubber, expected_params: dict, item: dict | None = None) -> None:
    stub.add_client_error(
        "update_item",
        service_error_code="ConditionalCheckFailedException",
        service_message="The conditional request failed",
        http_status_code=400,
        expected_params=expected_params,
        response_meta=None,
        modeled_fields={"Item": item} if item is not None else None,
    )


def claim_params() -> dict:
    return {
        "TableName": TABLE,
        "Key": KEY,
        "UpdateExpression": "SET #s = :deciding, approvedBy = :by",
        "ConditionExpression": "#s = :pending AND expiresAt > :now",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {
            ":deciding": {"S": "DECIDING"},
            ":pending": {"S": "PENDING"},
            ":now": {"S": NOW},
            ":by": APPROVER_ATTR,
        },
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


def finalize_params() -> dict:
    return {
        "TableName": TABLE,
        "Key": KEY,
        "UpdateExpression": "SET #s = :status, decidedAt = :at",
        "ConditionExpression": "#s = :deciding",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {
            ":status": {"S": "APPROVED"},
            ":at": {"S": NOW},
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
            ":at": {"S": NOW},
            ":pending": {"S": "PENDING"},
            ":deciding": {"S": "DECIDING"},
        },
    }


def test_expiry_is_71_hours_30_minutes_after_creation():
    created = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)

    assert APPROVAL_WINDOW == timedelta(hours=71, minutes=30)
    assert expiry_for(created) == datetime(2026, 11, 6, 11, 30, tzinfo=UTC)


def test_errors_share_a_base_class():
    for error in (ApprovalExistsError, AlreadyDecidedError, ExpiredError):
        assert issubclass(error, ApprovalError)


def test_create_writes_conditional_put_with_ttl(ddb_stub):
    ddb_stub.add_response(
        "put_item",
        {},
        {
            "TableName": TABLE,
            "Item": RECORD_ITEM,
            "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)",
        },
    )

    store(ddb_stub).create(RECORD, "tok")

    ddb_stub.assert_no_pending_responses()


def test_create_raises_when_record_exists(ddb_stub):
    ddb_stub.add_client_error(
        "put_item",
        service_error_code="ConditionalCheckFailedException",
        expected_params={
            "TableName": TABLE,
            "Item": RECORD_ITEM,
            "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)",
        },
    )

    with pytest.raises(ApprovalExistsError, match=f"^{ID_HASH}$"):
        store(ddb_stub).create(RECORD, "tok")
    ddb_stub.assert_no_pending_responses()


def test_create_propagates_other_errors(ddb_stub):
    ddb_stub.add_client_error(
        "put_item",
        service_error_code="ProvisionedThroughputExceededException",
        expected_params={
            "TableName": TABLE,
            "Item": RECORD_ITEM,
            "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)",
        },
    )

    with pytest.raises(ClientError):
        store(ddb_stub).create(RECORD, "tok")


def test_get_returns_record_and_token(ddb_stub):
    ddb_stub.add_response(
        "get_item",
        {"Item": RECORD_ITEM},
        {"TableName": TABLE, "Key": KEY, "ConsistentRead": True},
    )

    record, token = store(ddb_stub).get(APPROVAL_ID)

    assert record == RECORD
    assert token == RECORD_ITEM["taskToken"]["S"]
    ddb_stub.assert_no_pending_responses()


def test_get_round_trips_a_decided_record(ddb_stub):
    item = {
        **RECORD_ITEM,
        "status": {"S": "APPROVED"},
        "approvedBy": APPROVER_ATTR,
        "decidedAt": {"S": NOW},
    }
    ddb_stub.add_response(
        "get_item", {"Item": item}, {"TableName": TABLE, "Key": KEY, "ConsistentRead": True}
    )

    record, _ = store(ddb_stub).get(APPROVAL_ID)

    assert record.status == "APPROVED"
    assert record.approved_by == APPROVER
    assert record.decided_at == NOW


def test_get_returns_none_when_missing(ddb_stub):
    ddb_stub.add_response("get_item", {}, {"TableName": TABLE, "Key": KEY, "ConsistentRead": True})

    assert store(ddb_stub).get(APPROVAL_ID) is None
    ddb_stub.assert_no_pending_responses()


def test_claim_sets_deciding_with_pending_and_unexpired_condition(ddb_stub):
    ddb_stub.add_response("update_item", {}, claim_params())

    assert store(ddb_stub).claim(APPROVAL_ID, NOW, APPROVER) == "DECIDING"
    ddb_stub.assert_no_pending_responses()


def test_claim_raises_already_decided_when_old_status_is_not_pending(ddb_stub):
    conditional_failure(ddb_stub, claim_params(), {**RECORD_ITEM, "status": {"S": "APPROVED"}})

    with pytest.raises(AlreadyDecidedError, match=f"^{ID_HASH}$"):
        store(ddb_stub).claim(APPROVAL_ID, NOW, APPROVER)
    ddb_stub.assert_no_pending_responses()


def test_claim_raises_expired_when_old_status_is_pending(ddb_stub):
    conditional_failure(ddb_stub, claim_params(), RECORD_ITEM)

    with pytest.raises(ExpiredError, match=f"^{ID_HASH}$"):
        store(ddb_stub).claim(APPROVAL_ID, NOW, APPROVER)
    ddb_stub.assert_no_pending_responses()


def test_claim_raises_expired_when_the_old_item_is_missing(ddb_stub):
    conditional_failure(ddb_stub, claim_params())

    with pytest.raises(ExpiredError, match=f"^{ID_HASH}$"):
        store(ddb_stub).claim(APPROVAL_ID, NOW, APPROVER)


def test_claim_propagates_other_errors(ddb_stub):
    ddb_stub.add_client_error(
        "update_item",
        service_error_code="ProvisionedThroughputExceededException",
        expected_params=claim_params(),
    )

    with pytest.raises(ClientError):
        store(ddb_stub).claim(APPROVAL_ID, NOW, APPROVER)


def test_revert_puts_the_record_back_to_pending(ddb_stub):
    ddb_stub.add_response("update_item", {}, revert_params())

    store(ddb_stub).revert(APPROVAL_ID)

    ddb_stub.assert_no_pending_responses()


def test_revert_swallows_conditional_failure(ddb_stub, caplog):
    conditional_failure(ddb_stub, revert_params())

    with caplog.at_level(logging.WARNING, logger="shared.approvals"):
        store(ddb_stub).revert(APPROVAL_ID)

    assert any(
        r.levelno == logging.WARNING and r.name == "shared.approvals" for r in caplog.records
    )
    ddb_stub.assert_no_pending_responses()


def test_revert_propagates_other_errors(ddb_stub):
    ddb_stub.add_client_error(
        "update_item",
        service_error_code="ProvisionedThroughputExceededException",
        expected_params=revert_params(),
    )

    with pytest.raises(ClientError):
        store(ddb_stub).revert(APPROVAL_ID)


def test_finalize_records_the_decision(ddb_stub):
    ddb_stub.add_response("update_item", {}, finalize_params())

    store(ddb_stub).finalize(APPROVAL_ID, "APPROVED", NOW)

    ddb_stub.assert_no_pending_responses()


def test_finalize_raises_already_decided_on_conditional_failure(ddb_stub):
    conditional_failure(ddb_stub, finalize_params())

    with pytest.raises(AlreadyDecidedError, match=f"^{ID_HASH}$"):
        store(ddb_stub).finalize(APPROVAL_ID, "APPROVED", NOW)
    ddb_stub.assert_no_pending_responses()


def test_expire_accepts_pending_or_deciding(ddb_stub):
    ddb_stub.add_response("update_item", {}, expire_params())

    store(ddb_stub).expire(APPROVAL_ID, NOW)

    ddb_stub.assert_no_pending_responses()


def test_expire_swallows_conditional_failure(ddb_stub):
    conditional_failure(ddb_stub, expire_params())

    store(ddb_stub).expire(APPROVAL_ID, NOW)

    ddb_stub.assert_no_pending_responses()


def refresh_token_params() -> dict:
    return {
        "TableName": TABLE,
        "Key": KEY,
        "UpdateExpression": "SET taskToken = :t",
        "ConditionExpression": "#s = :pending",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":t": {"S": "new-token"}, ":pending": {"S": "PENDING"}},
    }


def test_refresh_token_replaces_the_token_of_a_pending_approval(ddb_stub):
    ddb_stub.add_response("update_item", {}, refresh_token_params())

    store(ddb_stub).refresh_token(APPROVAL_ID, "new-token")

    ddb_stub.assert_no_pending_responses()


def test_refresh_token_raises_already_decided_on_conditional_failure(ddb_stub):
    conditional_failure(ddb_stub, refresh_token_params())

    with pytest.raises(AlreadyDecidedError, match=f"^{ID_HASH}$"):
        store(ddb_stub).refresh_token(APPROVAL_ID, "new-token")
    ddb_stub.assert_no_pending_responses()


def test_refresh_token_propagates_other_errors(ddb_stub):
    ddb_stub.add_client_error(
        "update_item",
        service_error_code="ProvisionedThroughputExceededException",
        expected_params=refresh_token_params(),
    )

    with pytest.raises(ClientError):
        store(ddb_stub).refresh_token(APPROVAL_ID, "new-token")


def mark_reminded_params() -> dict:
    return {
        "TableName": TABLE,
        "Key": KEY,
        "UpdateExpression": "SET remindedAt = :now",
        "ConditionExpression": (
            "#s = :pending AND expiresAt > :now AND attribute_not_exists(remindedAt)"
        ),
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":now": {"S": NOW}, ":pending": {"S": "PENDING"}},
        "ReturnValues": "ALL_NEW",
    }


def test_mark_reminded_stamps_a_pending_approval_once(ddb_stub):
    ddb_stub.add_response(
        "update_item",
        {"Attributes": {**RECORD_ITEM, "remindedAt": {"S": NOW}}},
        mark_reminded_params(),
    )

    assert store(ddb_stub).mark_reminded(APPROVAL_ID, NOW) == RECORD
    ddb_stub.assert_no_pending_responses()


def test_mark_reminded_returns_none_when_the_condition_fails(ddb_stub):
    # Decided, expired or already reminded: there is nothing to remind about.
    conditional_failure(ddb_stub, mark_reminded_params())

    assert store(ddb_stub).mark_reminded(APPROVAL_ID, NOW) is None
    ddb_stub.assert_no_pending_responses()


def test_mark_reminded_raises_other_errors(ddb_stub):
    ddb_stub.add_client_error(
        "update_item",
        service_error_code="ProvisionedThroughputExceededException",
        expected_params=mark_reminded_params(),
    )

    with pytest.raises(ClientError):
        store(ddb_stub).mark_reminded(APPROVAL_ID, NOW)
