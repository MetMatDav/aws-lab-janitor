from dataclasses import replace

import pytest
from botocore.exceptions import UnknownServiceError
from botocore.stub import Stubber

from detector.detectors.monitor import detect_internet_monitor
from tests.unit.conftest import by_id, run_detector

MONITOR_ARN = "arn:aws:internetmonitor:us-east-1:123456789012:monitor/"


@pytest.fixture
def im_stub(stub_factory) -> Stubber:
    return stub_factory("internetmonitor")


def monitor(name, status="ACTIVE"):
    return {"MonitorName": name, "MonitorArn": MONITOR_ARN + name, "Status": status}


def stub_monitors(stub: Stubber, monitors) -> None:
    stub.add_response("list_monitors", {"Monitors": list(monitors)}, {})


def stub_monitor_tags(stub: Stubber, name: str, tags: dict | None) -> None:
    """Queue the tag lookup for ``name``; ``tags=None`` makes it fail."""
    params = {"ResourceArn": MONITOR_ARN + name}
    if tags is None:
        stub.add_client_error(
            "list_tags_for_resource",
            service_error_code="AccessDeniedException",
            http_status_code=403,
            expected_params=params,
        )
    else:
        stub.add_response("list_tags_for_resource", {"Tags": tags}, params)


def test_monitor_keep_tag_blocks_candidate(im_stub, ctx):
    stub_monitors(im_stub, [monitor("LAB-kept")])
    stub_monitor_tags(im_stub, "LAB-kept", {"janitor": "keep"})
    findings, errors = run_detector(detect_internet_monitor, ctx)
    assert errors == []
    finding = by_id(findings, "internet-monitor")["LAB-kept"]
    assert finding.lab is True and finding.candidate is False
    assert finding.reason == "tagged janitor=keep"
    assert finding.tags == [{"Key": "janitor", "Value": "keep"}]
    im_stub.assert_no_pending_responses()


def test_lab_monitor_is_candidate(im_stub, ctx):
    stub_monitors(im_stub, [monitor("LAB-watch", "PENDING"), monitor("prod-watch")])
    stub_monitor_tags(im_stub, "LAB-watch", {"Purpose": "lab"})
    findings, errors = run_detector(detect_internet_monitor, ctx)
    assert errors == []
    monitors = by_id(findings, "internet-monitor")
    lab = monitors["LAB-watch"]
    assert lab.candidate is True and lab.lab is True
    assert lab.reason == "name prefix LAB-"
    assert lab.arn == MONITOR_ARN + "LAB-watch"
    assert lab.detail == "PENDING"
    assert lab.age_days is None and lab.estimated_monthly_usd is None
    prod = monitors["prod-watch"]
    assert prod.lab is False and prod.candidate is False and prod.reason == ""
    im_stub.assert_no_pending_responses()


def test_monitor_without_prefix_skips_tag_lookup(im_stub, ctx):
    # Only LAB- monitors get a tag lookup; the Stubber fails on any unexpected call.
    stub_monitors(im_stub, [monitor("watch-demo")])
    findings, errors = run_detector(detect_internet_monitor, ctx)
    assert errors == []
    finding = by_id(findings, "internet-monitor")["watch-demo"]
    assert finding.lab is False and finding.candidate is False
    im_stub.assert_no_pending_responses()


def test_monitor_tag_failure_fails_closed(im_stub, ctx):
    stub_monitors(im_stub, [monitor("LAB-unknown"), monitor("LAB-ok")])
    stub_monitor_tags(im_stub, "LAB-unknown", None)
    stub_monitor_tags(im_stub, "LAB-ok", {})
    findings, errors = run_detector(detect_internet_monitor, ctx)
    assert [(e.service, e.classification) for e in errors] == [
        ("internetmonitor:ListTagsForResource", "access-denied")
    ]
    monitors = by_id(findings, "internet-monitor")
    unknown = monitors["LAB-unknown"]
    assert unknown.lab is True and unknown.candidate is False
    assert unknown.reason == "tags unknown"
    assert monitors["LAB-ok"].candidate is True
    im_stub.assert_no_pending_responses()


def test_candidate_reason_uses_configured_prefix(im_stub, ctx_factory, ctx):
    settings = replace(ctx.settings, name_prefix="TMP-")
    stub_monitors(im_stub, [monitor("TMP-watch"), monitor("LAB-watch")])
    stub_monitor_tags(im_stub, "TMP-watch", {})
    findings, errors = run_detector(detect_internet_monitor, ctx_factory(settings=settings))
    assert errors == []
    monitors = by_id(findings, "internet-monitor")
    assert monitors["TMP-watch"].reason == "name prefix TMP-"
    assert monitors["LAB-watch"].candidate is False
    im_stub.assert_no_pending_responses()


def test_list_monitors_failure_is_recorded(im_stub, ctx):
    im_stub.add_client_error(
        "list_monitors",
        service_error_code="AccessDeniedException",
        http_status_code=403,
        expected_params={},
    )
    findings, errors = run_detector(detect_internet_monitor, ctx)
    assert findings == []
    assert [e.service for e in errors] == ["internetmonitor:ListMonitors"]
    im_stub.assert_no_pending_responses()


def test_client_creation_failure_is_recorded_not_raised(ctx_factory):
    class BrokenClients:
        def get(self, service, region):
            raise UnknownServiceError(service_name=service, known_service_names="s3")

    findings, errors = run_detector(detect_internet_monitor, ctx_factory(clients=BrokenClients()))
    assert findings == []
    assert [(e.service, e.classification) for e in errors] == [
        ("internetmonitor:ListMonitors", "service-unavailable-in-region")
    ]


def test_past_deadline_makes_no_calls(im_stub, ctx_past_deadline):
    findings, errors = run_detector(detect_internet_monitor, ctx_past_deadline)
    assert findings == []
    assert [e.classification for e in errors] == ["time-budget"]
    im_stub.assert_no_pending_responses()
