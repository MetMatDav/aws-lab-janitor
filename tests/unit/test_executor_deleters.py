import pytest

from executor import deleters
from tests.unit.conftest import days_ago

REGION = "eu-west-1"
MONITOR_ARN = f"arn:aws:internetmonitor:{REGION}:123456789012:monitor/LAB-mon"


def item(kind: str, resource_id: str, region: str = REGION) -> dict:
    return {"region": region, "kind": kind, "resourceId": resource_id}


@pytest.mark.parametrize(
    ("kind", "resource_id", "service", "region", "operation", "params", "response"),
    [
        (
            "ebs-snapshot",
            "snap-1",
            "ec2",
            REGION,
            "delete_snapshot",
            {"SnapshotId": "snap-1"},
            {},
        ),
        ("ebs-volume", "vol-1", "ec2", REGION, "delete_volume", {"VolumeId": "vol-1"}, {}),
        (
            "elastic-ip",
            "eipalloc-1",
            "ec2",
            REGION,
            "release_address",
            {"AllocationId": "eipalloc-1"},
            {},
        ),
        (
            "cw-dashboard",
            "LAB-dash",
            "cloudwatch",
            "us-east-1",
            "delete_dashboards",
            {"DashboardNames": ["LAB-dash"]},
            {},
        ),
    ],
)
def test_each_kind_calls_its_delete_api(  # noqa: PLR0913, PLR0917
    stub_factory, fake_clients, kind, resource_id, service, region, operation, params, response
):
    stub = stub_factory(service, region)
    stub.add_response(operation, response, params)
    entry_region = "global" if kind == "cw-dashboard" else REGION

    deleters.delete(item(kind, resource_id, entry_region), fake_clients)

    stub.assert_no_pending_responses()


def get_monitor(status: str) -> dict:
    return {
        "MonitorName": "LAB-mon",
        "MonitorArn": MONITOR_ARN,
        "Resources": [],
        "Status": status,
        "CreatedAt": days_ago(10),
        "ModifiedAt": days_ago(10),
    }


NAME = {"MonitorName": "LAB-mon"}


def test_monitor_is_disabled_and_polled_before_delete(stub_factory, fake_clients):
    stub = stub_factory("internetmonitor", REGION)
    stub.add_response("get_monitor", get_monitor("ACTIVE"), NAME)
    stub.add_response(
        "update_monitor",
        {"MonitorArn": MONITOR_ARN, "Status": "INACTIVE"},
        {"MonitorName": "LAB-mon", "Status": "INACTIVE"},
    )
    stub.add_response("get_monitor", get_monitor("ACTIVE"), NAME)
    stub.add_response("get_monitor", get_monitor("INACTIVE"), NAME)
    stub.add_response("delete_monitor", {}, NAME)
    sleeps: list[float] = []

    deleters.delete(item("internet-monitor", "LAB-mon"), fake_clients, sleep=sleeps.append)

    assert sleeps == [5, 5]
    stub.assert_no_pending_responses()


def test_inactive_monitor_is_deleted_without_update(stub_factory, fake_clients):
    stub = stub_factory("internetmonitor", REGION)
    stub.add_response("get_monitor", get_monitor("INACTIVE"), NAME)
    stub.add_response("delete_monitor", {}, NAME)
    sleeps: list[float] = []

    deleters.delete(item("internet-monitor", "LAB-mon"), fake_clients, sleep=sleeps.append)

    assert sleeps == []
    stub.assert_no_pending_responses()


def test_monitor_still_active_after_60s_raises(stub_factory, fake_clients):
    stub = stub_factory("internetmonitor", REGION)
    stub.add_response("get_monitor", get_monitor("ACTIVE"), NAME)
    stub.add_response(
        "update_monitor",
        {"MonitorArn": MONITOR_ARN, "Status": "PENDING"},
        {"MonitorName": "LAB-mon", "Status": "INACTIVE"},
    )
    for _ in range(12):
        stub.add_response("get_monitor", get_monitor("ACTIVE"), NAME)
    sleeps: list[float] = []

    with pytest.raises(deleters.MonitorStillActive):
        deleters.delete(item("internet-monitor", "LAB-mon"), fake_clients, sleep=sleeps.append)

    assert sleeps == [5] * 12
    assert sum(sleeps) == deleters.MONITOR_WAIT_SECONDS
    stub.assert_no_pending_responses()
