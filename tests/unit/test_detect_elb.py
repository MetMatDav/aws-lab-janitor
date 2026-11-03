import pytest
from botocore.exceptions import UnknownServiceError
from botocore.stub import Stubber

from detector.detectors.elb import detect_elbv2
from tests.unit.conftest import by_id, days_ago, run_detector

LAB = [{"Key": "Purpose", "Value": "lab"}]
LB_ARN = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/"


@pytest.fixture
def elbv2_stub(stub_factory) -> Stubber:
    return stub_factory("elbv2")


def load_balancer(name, lb_type="application", *, days=4):
    prefix = {"application": "app", "network": "net", "gateway": "gwy"}[lb_type]
    return {
        "LoadBalancerName": name,
        "LoadBalancerArn": f"{LB_ARN}{prefix}/{name}/0123456789abcdef",
        "Type": lb_type,
        "Scheme": "internet-facing",
        "State": {"Code": "active"},
        "CreatedTime": days_ago(days),
    }


def stub_tags(stub: Stubber, lbs, tags=None, *, fail=False) -> None:
    """Queue one describe_tags call for ``lbs``; ``tags`` maps a name to its tag list."""
    params = {"ResourceArns": [lb["LoadBalancerArn"] for lb in lbs]}
    if fail:
        stub.add_client_error(
            "describe_tags",
            service_error_code="AccessDenied",
            http_status_code=403,
            expected_params=params,
        )
        return
    tags = tags or {}
    descriptions = []
    for lb in lbs:
        description = {"ResourceArn": lb["LoadBalancerArn"]}
        if lb["LoadBalancerName"] in tags:  # the API rejects an empty Tags list
            description["Tags"] = tags[lb["LoadBalancerName"]]
        descriptions.append(description)
    stub.add_response("describe_tags", {"TagDescriptions": descriptions}, params)


def test_elbv2_tags_in_batches_of_20(elbv2_stub, ctx):
    lbs = [load_balancer(f"lb-{i:02d}") for i in range(25)]
    elbv2_stub.add_response("describe_load_balancers", {"LoadBalancers": lbs}, {})
    stub_tags(elbv2_stub, lbs[:20], {"lb-03": LAB})
    stub_tags(elbv2_stub, lbs[20:], {"lb-24": LAB})
    findings, errors = run_detector(detect_elbv2, ctx)
    assert errors == []
    assert len(findings) == 25
    assert sorted(f.resource_id for f in findings if f.lab) == ["lb-03", "lb-24"]
    elbv2_stub.assert_no_pending_responses()


def test_elbv2_fields_and_cost_by_type(elbv2_stub, ctx):
    lbs = [
        load_balancer("LAB-alb"),
        load_balancer("LAB-nlb", "network"),
        load_balancer("LAB-gwlb", "gateway"),
        load_balancer("prod-alb", days=100),
    ]
    elbv2_stub.add_response("describe_load_balancers", {"LoadBalancers": lbs}, {})
    stub_tags(elbv2_stub, lbs)
    findings, errors = run_detector(detect_elbv2, ctx)
    assert errors == []
    elbs = by_id(findings, "elbv2")
    alb = elbs["LAB-alb"]
    assert alb.lab is True and alb.candidate is False and alb.reason == ""
    assert alb.arn == lbs[0]["LoadBalancerArn"]
    assert alb.detail == "application internet-facing active"
    assert alb.age_days == 4
    assert alb.estimated_monthly_usd == 16.43
    assert elbs["LAB-nlb"].estimated_monthly_usd == 16.43
    assert elbs["LAB-gwlb"].estimated_monthly_usd is None
    assert elbs["prod-alb"].lab is False and elbs["prod-alb"].age_days == 100
    elbv2_stub.assert_no_pending_responses()


def test_elbv2_failed_tag_batch_falls_back_to_prefix(elbv2_stub, ctx):
    lbs = [load_balancer(f"lb-{i:02d}") for i in range(20)] + [
        load_balancer("LAB-late"),
        load_balancer("tagged-late"),
    ]
    elbv2_stub.add_response("describe_load_balancers", {"LoadBalancers": lbs}, {})
    stub_tags(elbv2_stub, lbs[:20], fail=True)
    stub_tags(elbv2_stub, lbs[20:], {"tagged-late": LAB})
    findings, errors = run_detector(detect_elbv2, ctx)
    assert [(e.service, e.classification) for e in errors] == [
        ("elasticloadbalancing:DescribeTags", "access-denied")
    ]
    assert len(findings) == 22
    assert sorted(f.resource_id for f in findings if f.lab) == ["LAB-late", "tagged-late"]
    assert by_id(findings, "elbv2")["lb-00"].tags == []
    elbv2_stub.assert_no_pending_responses()


def test_elbv2_describe_failure_is_recorded(elbv2_stub, ctx):
    elbv2_stub.add_client_error(
        "describe_load_balancers",
        service_error_code="AccessDenied",
        http_status_code=403,
        expected_params={},
    )
    findings, errors = run_detector(detect_elbv2, ctx)
    assert findings == []
    assert [e.service for e in errors] == ["elasticloadbalancing:DescribeLoadBalancers"]
    elbv2_stub.assert_no_pending_responses()


def test_elbv2_without_load_balancers_makes_no_tag_call(elbv2_stub, ctx):
    elbv2_stub.add_response("describe_load_balancers", {"LoadBalancers": []}, {})
    findings, errors = run_detector(detect_elbv2, ctx)
    assert findings == [] and errors == []
    elbv2_stub.assert_no_pending_responses()


def test_client_creation_failure_is_recorded_not_raised(ctx_factory):
    class BrokenClients:
        def get(self, service, region):
            raise UnknownServiceError(service_name=service, known_service_names="s3")

    findings, errors = run_detector(detect_elbv2, ctx_factory(clients=BrokenClients()))
    assert findings == []
    assert [(e.service, e.classification) for e in errors] == [
        ("elasticloadbalancing:DescribeLoadBalancers", "service-unavailable-in-region")
    ]


def test_past_deadline_makes_no_calls(elbv2_stub, ctx_past_deadline):
    findings, errors = run_detector(detect_elbv2, ctx_past_deadline)
    assert findings == []
    assert [e.classification for e in errors] == ["time-budget"]
    elbv2_stub.assert_no_pending_responses()
