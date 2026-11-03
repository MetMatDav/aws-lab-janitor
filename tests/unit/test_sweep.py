import logging

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, UnknownServiceError

from detector.sweep import TimeBudgetExceeded, classify_error, collect, safe


def client_error(code: str, status: int = 400) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "m"}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "Op",
    )


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (client_error("AccessDenied", 403), "access-denied"),
        (client_error("AccessDeniedException", 400), "access-denied"),
        (client_error("UnauthorizedOperation", 403), "access-denied"),
        (client_error("OptInRequired", 401), "access-denied"),
        (EndpointConnectionError(endpoint_url="x"), "service-unavailable-in-region"),
        (client_error("NotFound", 404), "service-unavailable-in-region"),
        (client_error("InvalidAction", 400), "service-unavailable-in-region"),
        (
            UnknownServiceError(service_name="x", known_service_names="ec2"),
            "service-unavailable-in-region",
        ),
        (client_error("Throttling"), "throttled"),
        (client_error("ThrottlingException"), "throttled"),
        (client_error("RequestLimitExceeded", 503), "throttled"),
        (client_error("TooManyRequestsException", 429), "throttled"),
        (TimeBudgetExceeded("t"), "time-budget"),
        (client_error("InvalidParameterValue"), "other"),
        (ValueError("x"), "other"),
    ],
)
def test_classify_error(exc, expected):
    assert classify_error(exc) == expected


def test_safe_records_service_unavailable(ctx):
    errors = []

    def boom():
        raise EndpointConnectionError(endpoint_url="x")

    safe(ctx, errors, "ap-east-2", "internetmonitor:ListMonitors", boom)
    assert errors[0].classification == "service-unavailable-in-region"


def test_safe_never_raises_truncates_and_logs(ctx, caplog):
    errors = []

    def boom():
        raise RuntimeError("x" * 500)

    with caplog.at_level(logging.WARNING, logger="test"):
        safe(ctx, errors, "us-east-1", "ec2:DescribeVolumes", boom)
    assert len(errors) == 1
    error = errors[0]
    assert (error.region, error.service, error.classification) == (
        "us-east-1",
        "ec2:DescribeVolumes",
        "other",
    )
    assert error.error == "x" * 160
    assert any("ec2:DescribeVolumes" in r.getMessage() for r in caplog.records)


def test_safe_runs_fn_without_recording_on_success(ctx):
    errors, calls = [], []
    safe(ctx, errors, "us-east-1", "ec2:DescribeVolumes", lambda: calls.append(1))
    assert calls == [1] and errors == []


def test_time_budget_skips_calls(ctx_past_deadline):
    errors, calls = [], []
    safe(ctx_past_deadline, errors, "us-east-1", "ec2:DescribeVolumes", lambda: calls.append(1))
    assert calls == []
    assert errors[0].classification == "time-budget"
    assert errors[0].error == "skipped: time budget exhausted"


def test_collect_paginates_and_resolves_dotted_key(stub_factory):
    stub = stub_factory("cloudfront")
    stub.add_response(
        "list_distributions",
        {
            "DistributionList": {
                "Marker": "",
                "NextMarker": "page2",
                "MaxItems": 1,
                "IsTruncated": True,
                "Quantity": 1,
                "Items": [_distribution("E1")],
            }
        },
        {},
    )
    stub.add_response(
        "list_distributions",
        {
            "DistributionList": {
                "Marker": "page2",
                "MaxItems": 1,
                "IsTruncated": False,
                "Quantity": 1,
                "Items": [_distribution("E2")],
            }
        },
        {"Marker": "page2"},
    )
    client = stub.client
    items = collect(client, "list_distributions", "DistributionList.Items")
    assert [i["Id"] for i in items] == ["E1", "E2"]
    stub.assert_no_pending_responses()


def test_collect_calls_once_when_not_paginated(ec2_stub):
    ec2_stub.add_response(
        "describe_addresses", {"Addresses": [{"PublicIp": "203.0.113.4"}]}, {"Filters": []}
    )
    items = collect(ec2_stub.client, "describe_addresses", "Addresses", Filters=[])
    assert items == [{"PublicIp": "203.0.113.4"}]
    ec2_stub.assert_no_pending_responses()


def test_collect_returns_empty_list_when_key_missing(ec2_stub):
    ec2_stub.add_response("describe_volumes", {}, {})
    assert collect(ec2_stub.client, "describe_volumes", "Volumes") == []
    ec2_stub.assert_no_pending_responses()


def _distribution(dist_id: str) -> dict:
    """Minimal DistributionSummary satisfying the botocore model's required fields."""
    empty = {"Quantity": 0}
    return {
        "Id": dist_id,
        "ARN": f"arn:aws:cloudfront::123456789012:distribution/{dist_id}",
        "Status": "Deployed",
        "LastModifiedTime": "2026-01-01T00:00:00Z",
        "DomainName": f"{dist_id}.cloudfront.net",
        "Aliases": empty,
        "Origins": {
            "Quantity": 1,
            "Items": [{"Id": "o", "DomainName": "example.com"}],
        },
        "DefaultCacheBehavior": {"TargetOriginId": "o", "ViewerProtocolPolicy": "allow-all"},
        "CacheBehaviors": empty,
        "CustomErrorResponses": empty,
        "Comment": "",
        "PriceClass": "PriceClass_All",
        "Enabled": True,
        "ViewerCertificate": {},
        "Restrictions": {"GeoRestriction": {"RestrictionType": "none", "Quantity": 0}},
        "WebACLId": "",
        "HttpVersion": "http2",
        "IsIPV6Enabled": True,
        "Staging": False,
    }
