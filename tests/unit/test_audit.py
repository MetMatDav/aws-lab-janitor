import logging
from datetime import UTC, datetime

import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from shared.audit import AuditClient

TABLE = "audit"
RUN_ID = "20261102T120000Z-ab12"
AT = datetime(2026, 11, 2, 12, 0, 5, tzinfo=UTC)
CONDITION = "attribute_not_exists(pk) AND attribute_not_exists(sk)"


@pytest.fixture
def ddb_stub(stub_factory) -> Stubber:
    return stub_factory("dynamodb")


def audit(stub: Stubber) -> AuditClient:
    return AuditClient(stub.client, TABLE)


def event_item(**detail_attr) -> dict:
    return {
        "pk": {"S": f"RUN#{RUN_ID}"},
        "sk": {"S": "RESOURCE#us-east-1#i-0abc#PROPOSED"},
        "event": {"S": "PROPOSED"},
        "at": {"S": "2026-11-02T12:00:05+00:00"},
        "actor": {"S": "lambda:detector"},
        "dryRun": {"BOOL": False},
        "resourceId": {"S": "i-0abc"},
        "region": {"S": "us-east-1"},
        "detail": {"M": detail_attr},
    }


def put_event(client: AuditClient, detail: dict) -> None:
    client.put_run_event(
        run_id=RUN_ID,
        region="us-east-1",
        resource_id="i-0abc",
        event="PROPOSED",
        actor="lambda:detector",
        dry_run=False,
        detail=detail,
        at=AT,
    )


def put_params(item: dict) -> dict:
    return {"TableName": TABLE, "Item": item, "ConditionExpression": CONDITION}


def query_params(resource_id: str, start_key: dict | None = None) -> dict:
    params = {
        "TableName": TABLE,
        "IndexName": "by-resource",
        "KeyConditionExpression": "resourceId = :id",
        "FilterExpression": "#e = :e AND dryRun = :f",
        "ExpressionAttributeNames": {"#e": "event"},
        "ExpressionAttributeValues": {
            ":id": {"S": resource_id},
            ":e": {"S": "PROPOSED"},
            ":f": {"BOOL": False},
        },
    }
    if start_key is not None:
        params["ExclusiveStartKey"] = start_key
    return params


def test_put_run_event_is_conditional(ddb_stub):
    ddb_stub.add_response("put_item", {}, put_params(event_item(kind={"S": "ec2:instance"})))

    put_event(audit(ddb_stub), {"kind": "ec2:instance"})

    ddb_stub.assert_no_pending_responses()


def test_put_run_event_converts_floats_and_drops_none(ddb_stub):
    detail = {
        "kind": "ec2:volume",
        "estimatedMonthlyUsd": 0.08,
        "ageDays": 12,
        "reason": None,
        "nested": {"sizeGb": 1.5, "skip": None, "tags": ["a", 2.25]},
    }
    expected = event_item(
        kind={"S": "ec2:volume"},
        estimatedMonthlyUsd={"N": "0.08"},
        ageDays={"N": "12"},
        nested={
            "M": {
                "sizeGb": {"N": "1.5"},
                "tags": {"L": [{"S": "a"}, {"N": "2.25"}]},
            }
        },
    )
    ddb_stub.add_response("put_item", {}, put_params(expected))

    put_event(audit(ddb_stub), detail)

    ddb_stub.assert_no_pending_responses()


def test_conditional_failure_is_swallowed(ddb_stub, caplog):
    ddb_stub.add_client_error(
        "put_item",
        service_error_code="ConditionalCheckFailedException",
        expected_params=put_params(event_item()),
    )

    with caplog.at_level(logging.WARNING, logger="shared.audit"):
        put_event(audit(ddb_stub), {})

    assert any(r.levelno == logging.WARNING and r.name == "shared.audit" for r in caplog.records)
    ddb_stub.assert_no_pending_responses()


def test_other_put_errors_propagate(ddb_stub):
    ddb_stub.add_client_error(
        "put_item",
        service_error_code="ProvisionedThroughputExceededException",
        expected_params=put_params(event_item()),
    )

    with pytest.raises(ClientError) as raised:
        put_event(audit(ddb_stub), {})
    assert raised.value.response["Error"]["Code"] == "ProvisionedThroughputExceededException"
    ddb_stub.assert_no_pending_responses()


def test_put_run_summary_writes_the_summary_record(ddb_stub):
    item = {
        "pk": {"S": f"RUN#{RUN_ID}"},
        "sk": {"S": "RUN#SUMMARY"},
        "event": {"S": "SUMMARY"},
        "at": {"S": "2026-11-02T12:00:05+00:00"},
        "actor": {"S": "lambda:detector"},
        "detail": {
            "M": {
                "candidates": {"N": "3"},
                "estimatedMonthlyUsd": {"N": "4.2"},
                "dryRun": {"BOOL": True},
            }
        },
    }
    ddb_stub.add_response("put_item", {}, put_params(item))

    audit(ddb_stub).put_run_summary(
        RUN_ID,
        {"candidates": 3, "estimatedMonthlyUsd": 4.2, "dryRun": True, "note": None},
        AT,
    )

    ddb_stub.assert_no_pending_responses()


def test_put_run_summary_swallows_conditional_failure(ddb_stub):
    item = {
        "pk": {"S": f"RUN#{RUN_ID}"},
        "sk": {"S": "RUN#SUMMARY"},
        "event": {"S": "SUMMARY"},
        "at": {"S": "2026-11-02T12:00:05+00:00"},
        "actor": {"S": "lambda:detector"},
        "detail": {"M": {}},
    }
    ddb_stub.add_client_error(
        "put_item",
        service_error_code="ConditionalCheckFailedException",
        expected_params=put_params(item),
    )

    audit(ddb_stub).put_run_summary(RUN_ID, {}, AT)

    ddb_stub.assert_no_pending_responses()


def test_was_proposed_live_filters_dry_run(ddb_stub):
    ddb_stub.add_response(
        "query",
        {"Items": [{"resourceId": {"S": "i-0abc"}}], "Count": 1, "ScannedCount": 2},
        query_params("i-0abc"),
    )

    assert audit(ddb_stub).was_proposed_live("i-0abc") is True
    ddb_stub.assert_no_pending_responses()


def test_was_proposed_live_is_false_when_nothing_matches(ddb_stub):
    ddb_stub.add_response(
        "query", {"Items": [], "Count": 0, "ScannedCount": 3}, query_params("i-0abc")
    )

    assert audit(ddb_stub).was_proposed_live("i-0abc") is False
    ddb_stub.assert_no_pending_responses()


def test_was_proposed_live_pages_until_a_match(ddb_stub):
    page_one_end = {"pk": {"S": "RUN#a"}, "sk": {"S": "x"}, "resourceId": {"S": "i-0abc"}}
    page_two_end = {"pk": {"S": "RUN#b"}, "sk": {"S": "y"}, "resourceId": {"S": "i-0abc"}}
    ddb_stub.add_response(
        "query",
        {"Items": [], "Count": 0, "ScannedCount": 5, "LastEvaluatedKey": page_one_end},
        query_params("i-0abc"),
    )
    ddb_stub.add_response(
        "query",
        {
            "Items": [{"resourceId": {"S": "i-0abc"}}],
            "Count": 1,
            "ScannedCount": 1,
            "LastEvaluatedKey": page_two_end,
        },
        query_params("i-0abc", page_one_end),
    )

    assert audit(ddb_stub).was_proposed_live("i-0abc") is True
    ddb_stub.assert_no_pending_responses()


def test_was_proposed_live_is_false_when_all_pages_are_exhausted(ddb_stub):
    end = {"pk": {"S": "RUN#a"}, "sk": {"S": "x"}, "resourceId": {"S": "i-0abc"}}
    ddb_stub.add_response(
        "query",
        {"Items": [], "Count": 0, "ScannedCount": 5, "LastEvaluatedKey": end},
        query_params("i-0abc"),
    )
    ddb_stub.add_response(
        "query", {"Items": [], "Count": 0, "ScannedCount": 2}, query_params("i-0abc", end)
    )

    assert audit(ddb_stub).was_proposed_live("i-0abc") is False
    ddb_stub.assert_no_pending_responses()
