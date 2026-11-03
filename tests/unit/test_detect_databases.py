import pytest
from botocore.exceptions import UnknownServiceError
from botocore.stub import Stubber

from detector.detectors.databases import detect_elasticache, detect_rds
from tests.unit.conftest import by_id, days_ago, run_detector

LAB = [{"Key": "Purpose", "Value": "lab"}]
DB_ARN = "arn:aws:rds:us-east-1:123456789012:db:"
CLUSTER_ARN = "arn:aws:rds:us-east-1:123456789012:cluster:"
CACHE_ARN = "arn:aws:elasticache:us-east-1:123456789012:cluster:"


@pytest.fixture
def rds_stub(stub_factory) -> Stubber:
    return stub_factory("rds")


@pytest.fixture
def cache_stub(stub_factory) -> Stubber:
    return stub_factory("elasticache")


def stub_rds(stub: Stubber, instances=(), clusters=(), failures=()) -> None:
    """Queue both RDS calls in detector order; ``failures`` names operations that fail."""
    for op, key, items in (
        ("describe_db_instances", "DBInstances", instances),
        ("describe_db_clusters", "DBClusters", clusters),
    ):
        if op in failures:
            stub.add_client_error(
                op, service_error_code="AccessDenied", http_status_code=403, expected_params={}
            )
        else:
            stub.add_response(op, {key: list(items)}, {})


def db_instance(identifier, *, tags=(), days=12):
    return {
        "DBInstanceIdentifier": identifier,
        "DBInstanceArn": DB_ARN + identifier,
        "DBInstanceClass": "db.t4g.micro",
        "Engine": "postgres",
        "DBInstanceStatus": "available",
        "InstanceCreateTime": days_ago(days),
        "TagList": list(tags),
    }


def db_cluster(identifier, *, members=(), tags=(), days=30):
    return {
        "DBClusterIdentifier": identifier,
        "DBClusterArn": CLUSTER_ARN + identifier,
        "Engine": "aurora-postgresql",
        "Status": "available",
        "ClusterCreateTime": days_ago(days),
        "DBClusterMembers": [{"DBInstanceIdentifier": m} for m in members],
        "TagList": list(tags),
    }


def test_rds_tag_marks_lab_without_prefix(rds_stub, ctx):
    stub_rds(rds_stub, instances=[db_instance("postgres-demo", tags=LAB)])
    findings, errors = run_detector(detect_rds, ctx)
    finding = by_id(findings, "rds-instance")["postgres-demo"]
    assert finding.lab is True and finding.candidate is False
    assert errors == []
    rds_stub.assert_no_pending_responses()


def test_rds_instance_fields(rds_stub, ctx):
    stub_rds(rds_stub, instances=[db_instance("LAB-db"), db_instance("prod-db")])
    findings, errors = run_detector(detect_rds, ctx)
    assert errors == []
    instances = by_id(findings, "rds-instance")
    lab = instances["LAB-db"]
    assert lab.lab is True and lab.candidate is False and lab.reason == ""
    assert lab.arn == DB_ARN + "LAB-db"
    assert lab.detail == "db.t4g.micro postgres available"
    assert lab.age_days == 12 and lab.estimated_monthly_usd is None
    assert instances["prod-db"].lab is False
    rds_stub.assert_no_pending_responses()


def test_headless_cluster_is_reported(rds_stub, ctx):
    stub_rds(rds_stub, clusters=[db_cluster("LAB-aurora")])
    findings, errors = run_detector(detect_rds, ctx)
    assert errors == []
    finding = by_id(findings, "rds-cluster")["LAB-aurora"]
    assert finding.kind == "rds-cluster" and finding.detail.startswith("no instances")
    assert finding.detail.startswith("no instances: storage and backups still billed")
    assert finding.candidate is False and finding.lab is True
    assert finding.arn == CLUSTER_ARN + "LAB-aurora" and finding.age_days == 30
    rds_stub.assert_no_pending_responses()


def test_cluster_with_members_and_tags(rds_stub, ctx):
    stub_rds(rds_stub, clusters=[db_cluster("aurora-demo", members=["a", "b"], tags=LAB)])
    findings, errors = run_detector(detect_rds, ctx)
    assert errors == []
    finding = by_id(findings, "rds-cluster")["aurora-demo"]
    assert finding.detail == "aurora-postgresql available 2 instances"
    assert finding.lab is True and finding.candidate is False and finding.reason == ""
    rds_stub.assert_no_pending_responses()


def test_rds_failure_in_one_call_does_not_stop_the_other(rds_stub, ctx):
    stub_rds(rds_stub, clusters=[db_cluster("LAB-aurora")], failures={"describe_db_instances"})
    findings, errors = run_detector(detect_rds, ctx)
    assert [(e.service, e.classification) for e in errors] == [
        ("rds:DescribeDBInstances", "access-denied")
    ]
    assert list(by_id(findings, "rds-cluster")) == ["LAB-aurora"]
    rds_stub.assert_no_pending_responses()


def test_rds_every_call_failing_names_its_operation(rds_stub, ctx):
    stub_rds(rds_stub, failures={"describe_db_instances", "describe_db_clusters"})
    findings, errors = run_detector(detect_rds, ctx)
    assert findings == []
    assert [e.service for e in errors] == ["rds:DescribeDBInstances", "rds:DescribeDBClusters"]
    rds_stub.assert_no_pending_responses()


def cache_cluster(cluster_id, *, days=9):
    return {
        "CacheClusterId": cluster_id,
        "ARN": CACHE_ARN + cluster_id,
        "CacheNodeType": "cache.t4g.micro",
        "Engine": "redis",
        "CacheClusterStatus": "available",
        "CacheClusterCreateTime": days_ago(days),
    }


def stub_cache_tags(stub: Stubber, cluster_id: str, tags=None) -> None:
    """Queue the tag lookup for ``cluster_id``; ``tags=None`` makes it fail."""
    params = {"ResourceName": CACHE_ARN + cluster_id}
    if tags is None:
        stub.add_client_error(
            "list_tags_for_resource",
            service_error_code="AccessDenied",
            http_status_code=403,
            expected_params=params,
        )
    else:
        stub.add_response("list_tags_for_resource", {"TagList": list(tags)}, params)


def test_elasticache_tags_mark_lab(cache_stub, ctx):
    cache_stub.add_response(
        "describe_cache_clusters",
        {"CacheClusters": [cache_cluster("redis-demo"), cache_cluster("prod-cache")]},
        {},
    )
    stub_cache_tags(cache_stub, "redis-demo", LAB)
    stub_cache_tags(cache_stub, "prod-cache", [])
    findings, errors = run_detector(detect_elasticache, ctx)
    assert errors == []
    caches = by_id(findings, "elasticache")
    demo = caches["redis-demo"]
    assert demo.lab is True and demo.candidate is False and demo.reason == ""
    assert demo.arn == CACHE_ARN + "redis-demo"
    assert demo.detail == "cache.t4g.micro redis available"
    assert demo.age_days == 9 and demo.estimated_monthly_usd is None
    assert demo.tags == LAB
    assert caches["prod-cache"].lab is False
    cache_stub.assert_no_pending_responses()


def test_elasticache_tag_failure_falls_back_to_prefix(cache_stub, ctx):
    cache_stub.add_response(
        "describe_cache_clusters",
        {"CacheClusters": [cache_cluster("LAB-cache"), cache_cluster("redis-demo")]},
        {},
    )
    stub_cache_tags(cache_stub, "LAB-cache")
    stub_cache_tags(cache_stub, "redis-demo")
    findings, errors = run_detector(detect_elasticache, ctx)
    assert [e.service for e in errors] == ["elasticache:ListTagsForResource"] * 2
    caches = by_id(findings, "elasticache")
    assert caches["LAB-cache"].lab is True
    assert caches["redis-demo"].lab is False
    assert not any(f.candidate for f in findings)
    cache_stub.assert_no_pending_responses()


def test_elasticache_describe_failure_is_recorded(cache_stub, ctx):
    cache_stub.add_client_error(
        "describe_cache_clusters",
        service_error_code="AccessDenied",
        http_status_code=403,
        expected_params={},
    )
    findings, errors = run_detector(detect_elasticache, ctx)
    assert findings == []
    assert [e.service for e in errors] == ["elasticache:DescribeCacheClusters"]
    cache_stub.assert_no_pending_responses()


def test_client_creation_failure_is_recorded_not_raised(ctx_factory):
    class BrokenClients:
        def get(self, service, region):
            raise UnknownServiceError(service_name=service, known_service_names="s3")

    broken = ctx_factory(clients=BrokenClients())
    for detector, count in ((detect_rds, 2), (detect_elasticache, 1)):
        findings, errors = run_detector(detector, broken)
        assert findings == []
        assert len(errors) == count
        assert {e.classification for e in errors} == {"service-unavailable-in-region"}


def test_past_deadline_makes_no_calls(rds_stub, cache_stub, ctx_past_deadline):
    for detector in (detect_rds, detect_elasticache):
        findings, errors = run_detector(detector, ctx_past_deadline)
        assert findings == []
        assert {e.classification for e in errors} == {"time-budget"}
    rds_stub.assert_no_pending_responses()
    cache_stub.assert_no_pending_responses()
