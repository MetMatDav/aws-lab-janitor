from dataclasses import replace

import pytest
from botocore.exceptions import UnknownServiceError
from botocore.stub import Stubber

from detector.detectors.global_ import detect_global
from tests.unit.conftest import by_id, days_ago

ACCOUNT = "123456789012"
LAB = [{"Key": "Purpose", "Value": "lab"}]
DASHBOARD_ARN = f"arn:aws:cloudwatch::{ACCOUNT}:dashboard/"
DISTRIBUTION_ARN = f"arn:aws:cloudfront::{ACCOUNT}:distribution/"


@pytest.fixture
def s3_stub(stub_factory) -> Stubber:
    return stub_factory("s3")


@pytest.fixture
def cf_stub(stub_factory) -> Stubber:
    return stub_factory("cloudfront")


@pytest.fixture
def cw_stub(stub_factory) -> Stubber:
    return stub_factory("cloudwatch")


def run_global(ctx):
    findings, errors = [], []
    detect_global(ctx, findings, errors)
    return findings, errors


def bucket(name, days=30):
    return {"Name": name, "CreationDate": days_ago(days)}


def stub_buckets(stub: Stubber, buckets=()) -> None:
    stub.add_response("list_buckets", {"Buckets": list(buckets)}, {})


def stub_bucket_tags(stub: Stubber, name: str, tags) -> None:
    """Queue the tag lookup for ``name``; ``tags`` may also be an error code that makes it fail."""
    params = {"Bucket": name}
    if isinstance(tags, str):
        status = 404 if tags == "NoSuchTagSet" else 403
        stub.add_client_error(
            "get_bucket_tagging",
            service_error_code=tags,
            http_status_code=status,
            expected_params=params,
        )
    else:
        stub.add_response("get_bucket_tagging", {"TagSet": list(tags)}, params)


def distribution(dist_id, domain, enabled=True, days=10):
    return {
        "Id": dist_id,
        "ARN": DISTRIBUTION_ARN + dist_id,
        "Status": "Deployed",
        "LastModifiedTime": days_ago(days),
        "DomainName": domain,
        "Aliases": {"Quantity": 0},
        "Origins": {"Quantity": 1, "Items": [{"Id": "origin", "DomainName": "origin.example"}]},
        "DefaultCacheBehavior": {"TargetOriginId": "origin", "ViewerProtocolPolicy": "allow-all"},
        "CacheBehaviors": {"Quantity": 0},
        "CustomErrorResponses": {"Quantity": 0},
        "Comment": "",
        "PriceClass": "PriceClass_All",
        "Enabled": enabled,
        "ViewerCertificate": {},
        "Restrictions": {"GeoRestriction": {"RestrictionType": "none", "Quantity": 0}},
        "WebACLId": "",
        "HttpVersion": "http2",
        "IsIPV6Enabled": True,
        "Staging": False,
    }


def stub_distributions(stub: Stubber, *pages) -> None:
    """Queue one ListDistributions call per page; no pages queues a single empty page."""
    pages = pages or ((),)
    for index, items in enumerate(pages):
        last = index == len(pages) - 1
        listing = {
            "Marker": f"m{index}" if index else "",
            "MaxItems": 100,
            "IsTruncated": not last,
            "Quantity": len(items),
            "Items": list(items),
        }
        if not last:
            listing["NextMarker"] = f"m{index + 1}"
        params = {"Marker": f"m{index}"} if index else {}
        stub.add_response("list_distributions", {"DistributionList": listing}, params)


def dashboard(name, days=5, arn=True):
    entry = {"DashboardName": name, "LastModified": days_ago(days), "Size": 100}
    if arn:
        entry["DashboardArn"] = DASHBOARD_ARN + name
    return entry


def stub_dashboards(stub: Stubber, dashboards=()) -> None:
    stub.add_response("list_dashboards", {"DashboardEntries": list(dashboards)}, {})


def stub_dashboard_tags(stub: Stubber, name: str, tags) -> None:
    """Queue the tag lookup for ``name``; ``tags=None`` makes it fail."""
    params = {"ResourceARN": DASHBOARD_ARN + name}
    if tags is None:
        stub.add_client_error(
            "list_tags_for_resource",
            service_error_code="AccessDenied",
            http_status_code=403,
            expected_params=params,
        )
    else:
        stub.add_response("list_tags_for_resource", {"Tags": list(tags)}, params)


def assert_drained(*stubs: Stubber) -> None:
    for stub in stubs:
        stub.assert_no_pending_responses()


def test_cdk_buckets_are_skipped_and_tags_read(s3_stub, cf_stub, cw_stub, ctx):
    # No tag lookup is queued for the cdk- bucket; the Stubber fails on any unexpected call.
    stub_buckets(s3_stub, [bucket("cdk-hnb659fds-assets"), bucket("team-data", 40)])
    stub_bucket_tags(s3_stub, "team-data", LAB)
    stub_distributions(cf_stub)
    stub_dashboards(cw_stub)
    findings, errors = run_global(ctx)
    assert errors == []
    buckets = by_id(findings, "s3-bucket")
    assert list(buckets) == ["team-data"]
    team = buckets["team-data"]
    assert team.region == "global"
    assert team.lab is True and team.candidate is False and team.reason == ""
    assert team.arn == "arn:aws:s3:::team-data"
    assert team.detail == "bucket"
    assert team.tags == LAB
    assert team.age_days == 40 and team.estimated_monthly_usd is None
    assert_drained(s3_stub, cf_stub, cw_stub)


def test_no_such_tag_set_means_no_tags_not_error(s3_stub, cf_stub, cw_stub, ctx):
    stub_buckets(s3_stub, [bucket("prod-logs")])
    stub_bucket_tags(s3_stub, "prod-logs", "NoSuchTagSet")
    stub_distributions(cf_stub)
    stub_dashboards(cw_stub)
    findings, errors = run_global(ctx)
    assert errors == []
    prod = by_id(findings, "s3-bucket")["prod-logs"]
    assert prod.lab is False and prod.candidate is False and prod.tags == []
    assert_drained(s3_stub, cf_stub, cw_stub)


def test_bucket_tag_failure_falls_back_to_prefix(s3_stub, cf_stub, cw_stub, ctx):
    stub_buckets(s3_stub, [bucket("lab-scratch"), bucket("team-data")])
    stub_bucket_tags(s3_stub, "lab-scratch", "AccessDenied")
    stub_bucket_tags(s3_stub, "team-data", "AccessDenied")
    stub_distributions(cf_stub)
    stub_dashboards(cw_stub)
    findings, errors = run_global(ctx)
    assert [(e.region, e.service, e.classification) for e in errors] == [
        ("global", "s3:GetBucketTagging", "access-denied"),
        ("global", "s3:GetBucketTagging", "access-denied"),
    ]
    buckets = by_id(findings, "s3-bucket")
    assert buckets["lab-scratch"].lab is True and buckets["lab-scratch"].candidate is False
    assert buckets["team-data"].lab is False and buckets["team-data"].tags == []
    assert_drained(s3_stub, cf_stub, cw_stub)


def test_bucket_prefix_is_case_insensitive_but_tag_is_exact(s3_stub, cf_stub, cw_stub, ctx):
    stub_buckets(s3_stub, [bucket("lab-assets"), bucket("labrador"), bucket("team-data")])
    stub_bucket_tags(s3_stub, "lab-assets", [])
    stub_bucket_tags(s3_stub, "labrador", [])
    stub_bucket_tags(s3_stub, "team-data", [{"Key": "Purpose", "Value": "LAB"}])
    stub_distributions(cf_stub)
    stub_dashboards(cw_stub)
    findings, errors = run_global(ctx)
    assert errors == []
    buckets = by_id(findings, "s3-bucket")
    assert buckets["lab-assets"].lab is True
    assert buckets["labrador"].lab is False
    assert buckets["team-data"].lab is False
    assert all(not b.candidate for b in buckets.values())
    assert_drained(s3_stub, cf_stub, cw_stub)


def test_cloudfront_pagination(s3_stub, cf_stub, cw_stub, ctx):
    stub_buckets(s3_stub)
    stub_distributions(
        cf_stub,
        [distribution("E1", "d1.cloudfront.net"), distribution("E2", "d2.cloudfront.net", False)],
        [distribution("E3", "d3.cloudfront.net", days=3)],
    )
    stub_dashboards(cw_stub)
    findings, errors = run_global(ctx)
    assert errors == []
    distributions = by_id(findings, "cloudfront")
    assert list(distributions) == ["E1", "E2", "E3"]
    first = distributions["E1"]
    assert first.region == "global"
    assert first.arn == DISTRIBUTION_ARN + "E1"
    assert first.detail == "d1.cloudfront.net enabled=True"
    assert distributions["E2"].detail == "d2.cloudfront.net enabled=False"
    assert first.age_days == 10 and distributions["E3"].age_days == 3
    assert all(not d.lab and not d.candidate for d in distributions.values())
    assert all(d.estimated_monthly_usd is None for d in distributions.values())
    assert_drained(s3_stub, cf_stub, cw_stub)


def test_dashboard_candidate_and_keep(s3_stub, cf_stub, cw_stub, ctx):
    # Only LAB- dashboards (case-sensitive) get a tag lookup.
    stub_buckets(s3_stub)
    stub_distributions(cf_stub)
    stub_dashboards(
        cw_stub,
        [dashboard("LAB-board", 9), dashboard("LAB-kept"), dashboard("ops"), dashboard("lab-x")],
    )
    stub_dashboard_tags(cw_stub, "LAB-board", LAB)
    stub_dashboard_tags(cw_stub, "LAB-kept", [{"Key": "janitor", "Value": "keep"}])
    findings, errors = run_global(ctx)
    assert errors == []
    dashboards = by_id(findings, "cw-dashboard")
    board = dashboards["LAB-board"]
    assert board.region == "global"
    assert board.lab is True and board.candidate is True
    assert board.reason == "name prefix LAB-"
    assert board.arn == DASHBOARD_ARN + "LAB-board"
    assert board.detail == "dashboard"
    assert board.tags == LAB
    assert board.age_days == 9 and board.estimated_monthly_usd is None
    kept = dashboards["LAB-kept"]
    assert kept.lab is True and kept.candidate is False
    assert kept.reason == "tagged janitor=keep"
    for name in ("ops", "lab-x"):
        other = dashboards[name]
        assert other.lab is False and other.candidate is False and other.reason == ""
    assert_drained(s3_stub, cf_stub, cw_stub)


def test_dashboard_tag_failure_fails_closed(s3_stub, cf_stub, cw_stub, ctx):
    stub_buckets(s3_stub)
    stub_distributions(cf_stub)
    stub_dashboards(cw_stub, [dashboard("LAB-unknown")])
    stub_dashboard_tags(cw_stub, "LAB-unknown", None)
    findings, errors = run_global(ctx)
    assert [(e.region, e.service, e.classification) for e in errors] == [
        ("global", "cloudwatch:ListTagsForResource", "access-denied")
    ]
    unknown = by_id(findings, "cw-dashboard")["LAB-unknown"]
    assert unknown.lab is True and unknown.candidate is False
    assert unknown.reason == "tags unknown"
    assert_drained(s3_stub, cf_stub, cw_stub)


def test_dashboard_arn_prefers_api_value_and_falls_back_to_account(
    s3_stub, cf_stub, cw_stub, ctx_factory
):
    other_account = "111122223333"
    built_arn = f"arn:aws:cloudwatch::{other_account}:dashboard/LAB-built"
    stub_buckets(s3_stub)
    stub_distributions(cf_stub)
    stub_dashboards(cw_stub, [dashboard("LAB-built", arn=False), dashboard("LAB-api")])
    cw_stub.add_response("list_tags_for_resource", {"Tags": []}, {"ResourceARN": built_arn})
    stub_dashboard_tags(cw_stub, "LAB-api", [])
    findings, errors = run_global(ctx_factory(account=other_account))
    assert errors == []
    dashboards = by_id(findings, "cw-dashboard")
    assert dashboards["LAB-built"].arn == built_arn
    assert dashboards["LAB-api"].arn == DASHBOARD_ARN + "LAB-api"
    assert all(d.candidate for d in dashboards.values())
    assert_drained(s3_stub, cf_stub, cw_stub)


def test_dashboard_reason_uses_configured_prefix(s3_stub, cf_stub, cw_stub, ctx_factory, ctx):
    settings = replace(ctx.settings, name_prefix="TMP-")
    stub_buckets(s3_stub, [bucket("tmp-data")])
    stub_bucket_tags(s3_stub, "tmp-data", [])
    stub_distributions(cf_stub)
    stub_dashboards(cw_stub, [dashboard("TMP-board"), dashboard("LAB-board")])
    stub_dashboard_tags(cw_stub, "TMP-board", [])
    findings, errors = run_global(ctx_factory(settings=settings))
    assert errors == []
    assert by_id(findings, "s3-bucket")["tmp-data"].lab is True
    dashboards = by_id(findings, "cw-dashboard")
    assert dashboards["TMP-board"].reason == "name prefix TMP-"
    assert dashboards["LAB-board"].lab is False and dashboards["LAB-board"].candidate is False
    assert_drained(s3_stub, cf_stub, cw_stub)


def test_list_failure_does_not_stop_other_services(s3_stub, cf_stub, cw_stub, ctx):
    s3_stub.add_client_error(
        "list_buckets", service_error_code="AccessDenied", http_status_code=403, expected_params={}
    )
    stub_distributions(cf_stub, [distribution("E1", "d1.cloudfront.net")])
    stub_dashboards(cw_stub, [dashboard("ops")])
    findings, errors = run_global(ctx)
    assert [(e.region, e.service, e.classification) for e in errors] == [
        ("global", "s3:ListBuckets", "access-denied")
    ]
    assert sorted(f.kind for f in findings) == ["cloudfront", "cw-dashboard"]
    assert_drained(s3_stub, cf_stub, cw_stub)


def test_client_creation_failure_is_recorded_not_raised(ctx_factory):
    class BrokenClients:
        def get(self, service, region):
            raise UnknownServiceError(service_name=service, known_service_names="ec2")

    findings, errors = run_global(ctx_factory(clients=BrokenClients()))
    assert findings == []
    assert [(e.region, e.service, e.classification) for e in errors] == [
        ("global", "s3:ListBuckets", "service-unavailable-in-region"),
        ("global", "cloudfront:ListDistributions", "service-unavailable-in-region"),
        ("global", "cloudwatch:ListDashboards", "service-unavailable-in-region"),
    ]


def test_clients_are_requested_in_us_east_1(ctx_factory):
    requested = []

    class RecordingClients:
        def get(self, service, region):
            requested.append((service, region))
            raise UnknownServiceError(service_name=service, known_service_names="ec2")

    run_global(ctx_factory(clients=RecordingClients()))
    assert requested == [
        ("s3", "us-east-1"),
        ("cloudfront", "us-east-1"),
        ("cloudwatch", "us-east-1"),
    ]


def test_past_deadline_makes_no_calls(s3_stub, cf_stub, cw_stub, ctx_past_deadline):
    findings, errors = run_global(ctx_past_deadline)
    assert findings == []
    assert [(e.service, e.classification) for e in errors] == [
        ("s3:ListBuckets", "time-budget"),
        ("cloudfront:ListDistributions", "time-budget"),
        ("cloudwatch:ListDashboards", "time-budget"),
    ]
    assert_drained(s3_stub, cf_stub, cw_stub)
