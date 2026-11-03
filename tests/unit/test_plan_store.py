import io
import json

import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from shared.plan import plan_bytes, plan_sha256
from shared.plan_store import PlanStore, StoredPlan

BUCKET = "plans-bucket"
RUN_ID = "20261102T120000Z-ab12"
PLAN_KEY = f"plans/{RUN_ID}/plan.json"
REPORT_KEY = f"plans/{RUN_ID}/report.txt"
PLAN = {"runId": RUN_ID, "candidates": [{"resourceId": "i-1"}], "estimatedMonthlyUsd": 1.5}
REPORT = "Janitor report: 1 candidate - café"


@pytest.fixture
def s3_stub(stub_factory) -> Stubber:
    return stub_factory("s3")


def store(stub: Stubber) -> PlanStore:
    return PlanStore(stub.client, BUCKET)


def body(data: bytes) -> StreamingBody:
    return StreamingBody(io.BytesIO(data), len(data))


def test_put_writes_both_objects_and_returns_versions(s3_stub):
    data = plan_bytes(PLAN)
    s3_stub.add_response(
        "put_object",
        {"VersionId": "plan-v1"},
        {
            "Bucket": BUCKET,
            "Key": PLAN_KEY,
            "Body": data,
            "ContentType": "application/json",
        },
    )
    s3_stub.add_response(
        "put_object",
        {"VersionId": "report-v1"},
        {
            "Bucket": BUCKET,
            "Key": REPORT_KEY,
            "Body": REPORT.encode("utf-8"),
            "ContentType": "text/plain; charset=utf-8",
        },
    )

    stored = store(s3_stub).put(RUN_ID, PLAN, REPORT)

    assert stored == StoredPlan(
        plan_key=PLAN_KEY,
        plan_version_id="plan-v1",
        plan_sha256=plan_sha256(data),
        report_key=REPORT_KEY,
        report_version_id="report-v1",
    )
    s3_stub.assert_no_pending_responses()


def test_put_rejects_an_unversioned_bucket(s3_stub):
    s3_stub.add_response(
        "put_object",
        {},
        {
            "Bucket": BUCKET,
            "Key": PLAN_KEY,
            "Body": plan_bytes(PLAN),
            "ContentType": "application/json",
        },
    )

    with pytest.raises(RuntimeError, match="plans bucket is not versioned"):
        store(s3_stub).put(RUN_ID, PLAN, REPORT)
    s3_stub.assert_no_pending_responses()


def test_put_rejects_an_unversioned_bucket_on_the_report(s3_stub):
    s3_stub.add_response(
        "put_object",
        {"VersionId": "plan-v1"},
        {
            "Bucket": BUCKET,
            "Key": PLAN_KEY,
            "Body": plan_bytes(PLAN),
            "ContentType": "application/json",
        },
    )
    s3_stub.add_response(
        "put_object",
        {},
        {
            "Bucket": BUCKET,
            "Key": REPORT_KEY,
            "Body": REPORT.encode("utf-8"),
            "ContentType": "text/plain; charset=utf-8",
        },
    )

    with pytest.raises(RuntimeError, match="plans bucket is not versioned"):
        store(s3_stub).put(RUN_ID, PLAN, REPORT)
    s3_stub.assert_no_pending_responses()


def test_get_plan_reads_the_exact_version_and_hashes_the_bytes_read(s3_stub):
    data = plan_bytes(PLAN)
    s3_stub.add_response(
        "get_object",
        {"Body": body(data)},
        {"Bucket": BUCKET, "Key": PLAN_KEY, "VersionId": "plan-v1"},
    )

    plan, sha = store(s3_stub).get_plan(RUN_ID, "plan-v1")

    assert plan == PLAN
    assert sha == plan_sha256(data)
    s3_stub.assert_no_pending_responses()


def test_get_plan_hash_reflects_tampered_bytes(s3_stub):
    tampered = json.dumps(PLAN, indent=2).encode()
    s3_stub.add_response(
        "get_object",
        {"Body": body(tampered)},
        {"Bucket": BUCKET, "Key": PLAN_KEY, "VersionId": "plan-v1"},
    )

    plan, sha = store(s3_stub).get_plan(RUN_ID, "plan-v1")

    assert plan == PLAN
    assert sha != plan_sha256(plan_bytes(PLAN))
    assert sha == plan_sha256(tampered)
    s3_stub.assert_no_pending_responses()


def test_get_report_reads_the_exact_version_as_text(s3_stub):
    s3_stub.add_response(
        "get_object",
        {"Body": body(REPORT.encode("utf-8"))},
        {"Bucket": BUCKET, "Key": REPORT_KEY, "VersionId": "report-v1"},
    )

    assert store(s3_stub).get_report(RUN_ID, "report-v1") == REPORT
    s3_stub.assert_no_pending_responses()
