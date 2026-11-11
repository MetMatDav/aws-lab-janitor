import os

import pytest
from botocore.exceptions import ClientError

from executor import verify
from shared.settings import load_settings
from tests.unit.conftest import NOW, days_ago

LAB = [{"Key": "Purpose", "Value": "lab"}]
KEEP = [*LAB, {"Key": "janitor", "Value": "keep"}]
REGION = "eu-west-1"
DASHBOARD_ARN = "arn:aws:cloudwatch::123456789012:dashboard/LAB-dash"


def item(kind: str, resource_id: str, region: str = REGION, arn: str | None = None) -> dict:
    return {
        "region": region,
        "kind": kind,
        "resourceId": resource_id,
        "arn": arn,
        "detail": "",
        "lab": True,
        "candidate": True,
        "eligible": False,
        "reason": "planned",
        "ageDays": 42,
        "estimatedMonthlyUsd": 1.0,
    }


def recheck(entry: dict, fake_clients) -> str | None:
    return verify.recheck(entry, fake_clients, load_settings({}, os.environ), NOW)


def snapshot(snap_id="snap-1", *, days=42, tags=LAB, state="completed") -> dict:
    return {
        "SnapshotId": snap_id,
        "VolumeId": "vol-src",
        "VolumeSize": 8,
        "StartTime": days_ago(days),
        "State": state,
        "Tags": tags,
    }


def images_params(snap_id: str = "snap-1") -> dict:
    return {
        "Owners": ["self"],
        "IncludeDisabled": True,
        "Filters": [{"Name": "block-device-mapping.snapshot-id", "Values": [snap_id]}],
    }


def test_snapshot_still_deletable_returns_none(stub_factory, fake_clients):
    ec2 = stub_factory("ec2", REGION)
    ec2.add_response("describe_snapshots", {"Snapshots": [snapshot()]}, {"SnapshotIds": ["snap-1"]})
    ec2.add_response("describe_images", {"Images": []}, images_params())

    assert recheck(item("ebs-snapshot", "snap-1"), fake_clients) is None
    ec2.assert_no_pending_responses()


def test_snapshot_now_backs_an_ami(stub_factory, fake_clients):
    ec2 = stub_factory("ec2", REGION)
    ec2.add_response("describe_snapshots", {"Snapshots": [snapshot()]}, {"SnapshotIds": ["snap-1"]})
    ec2.add_response("describe_images", {"Images": [{"ImageId": "ami-9"}]}, images_params())

    assert recheck(item("ebs-snapshot", "snap-1"), fake_clients) == "backs AMI ami-9"
    ec2.assert_no_pending_responses()


def test_snapshot_gone_is_already_gone(stub_factory, fake_clients):
    ec2 = stub_factory("ec2", REGION)
    ec2.add_client_error(
        "describe_snapshots",
        service_error_code="InvalidSnapshot.NotFound",
        http_status_code=400,
        expected_params={"SnapshotIds": ["snap-1"]},
    )

    assert recheck(item("ebs-snapshot", "snap-1"), fake_clients) == "already-gone"
    ec2.assert_no_pending_responses()


@pytest.mark.parametrize(
    ("described", "reason"),
    [
        (snapshot(tags=KEEP), "tagged janitor=keep"),
        (snapshot(tags=[]), "not-lab-tagged"),
        (snapshot(tags=[{"Key": "Purpose", "Value": "prod"}]), "not-lab-tagged"),
        (snapshot(days=3), "below minimum age (3 days)"),
        (snapshot(state="pending"), "state pending"),
        (snapshot(state="pending", days=3), "state pending"),
    ],
)
def test_snapshot_kept_or_untagged_or_young_or_pending(
    stub_factory, fake_clients, described, reason
):
    ec2 = stub_factory("ec2", REGION)
    ec2.add_response("describe_snapshots", {"Snapshots": [described]}, {"SnapshotIds": ["snap-1"]})

    assert recheck(item("ebs-snapshot", "snap-1"), fake_clients) == reason
    ec2.assert_no_pending_responses()


def test_snapshot_unexpected_error_propagates(stub_factory, fake_clients):
    ec2 = stub_factory("ec2", REGION)
    ec2.add_client_error(
        "describe_snapshots",
        service_error_code="UnauthorizedOperation",
        http_status_code=403,
        expected_params={"SnapshotIds": ["snap-1"]},
    )

    with pytest.raises(ClientError, match="UnauthorizedOperation"):
        recheck(item("ebs-snapshot", "snap-1"), fake_clients)
    ec2.assert_no_pending_responses()


def volume(*, days=42, tags=LAB, state="available", attachments=()) -> dict:
    return {
        "VolumeId": "vol-1",
        "Size": 10,
        "State": state,
        "VolumeType": "gp3",
        "CreateTime": days_ago(days),
        "Attachments": list(attachments),
        "Tags": tags,
    }


ATTACHMENT = {"InstanceId": "i-1", "VolumeId": "vol-1", "State": "attached"}


@pytest.mark.parametrize(
    ("described", "reason"),
    [
        (volume(), None),
        (volume(attachments=[ATTACHMENT], state="in-use"), "attached"),
        (volume(state="creating"), "state creating"),
        (volume(tags=[]), "not-lab-tagged"),
        (volume(tags=KEEP, attachments=[ATTACHMENT]), "tagged janitor=keep"),
        (volume(days=1), "below minimum age (1 days)"),
    ],
)
def test_volume_attached_or_not_available(stub_factory, fake_clients, described, reason):
    ec2 = stub_factory("ec2", REGION)
    ec2.add_response("describe_volumes", {"Volumes": [described]}, {"VolumeIds": ["vol-1"]})

    assert recheck(item("ebs-volume", "vol-1"), fake_clients) == reason
    ec2.assert_no_pending_responses()


def test_volume_gone_is_already_gone(stub_factory, fake_clients):
    ec2 = stub_factory("ec2", REGION)
    ec2.add_client_error(
        "describe_volumes",
        service_error_code="InvalidVolume.NotFound",
        http_status_code=400,
        expected_params={"VolumeIds": ["vol-1"]},
    )

    assert recheck(item("ebs-volume", "vol-1"), fake_clients) == "already-gone"
    ec2.assert_no_pending_responses()


def address(**extra) -> dict:
    return {"AllocationId": "eipalloc-1", "PublicIp": "198.51.100.7", "Tags": LAB, **extra}


@pytest.mark.parametrize(
    ("described", "reason"),
    [
        (address(), None),
        (address(AssociationId="eipassoc-1"), "associated"),
        (address(Tags=KEEP, AssociationId="eipassoc-1"), "tagged janitor=keep"),
        (address(Tags=[]), "not-lab-tagged"),
    ],
)
def test_eip_associated(stub_factory, fake_clients, described, reason):
    ec2 = stub_factory("ec2", REGION)
    ec2.add_response(
        "describe_addresses", {"Addresses": [described]}, {"AllocationIds": ["eipalloc-1"]}
    )

    assert recheck(item("elastic-ip", "eipalloc-1"), fake_clients) == reason
    ec2.assert_no_pending_responses()


def test_eip_gone_is_already_gone(stub_factory, fake_clients):
    ec2 = stub_factory("ec2", REGION)
    ec2.add_client_error(
        "describe_addresses",
        service_error_code="InvalidAllocationID.NotFound",
        http_status_code=400,
        expected_params={"AllocationIds": ["eipalloc-1"]},
    )

    assert recheck(item("elastic-ip", "eipalloc-1"), fake_clients) == "already-gone"
    ec2.assert_no_pending_responses()


def dashboard(name: str = "LAB-dash") -> dict:
    return item("cw-dashboard", name, region="global", arn=DASHBOARD_ARN)


@pytest.mark.parametrize(
    ("tags", "reason"),
    [
        ([], None),
        (LAB, None),
        ([{"Key": "janitor", "Value": "keep"}], "tagged janitor=keep"),
    ],
)
def test_dashboard_prefix_and_keep_tag(stub_factory, fake_clients, tags, reason):
    cloudwatch = stub_factory("cloudwatch", "us-east-1")
    cloudwatch.add_response(
        "list_tags_for_resource", {"Tags": tags}, {"ResourceARN": DASHBOARD_ARN}
    )

    assert recheck(dashboard(), fake_clients) == reason
    cloudwatch.assert_no_pending_responses()


def test_dashboard_without_prefix_makes_no_call(stub_factory, fake_clients):
    cloudwatch = stub_factory("cloudwatch", "us-east-1")

    assert recheck(dashboard("prod-dash"), fake_clients) == "name prefix mismatch"
    cloudwatch.assert_no_pending_responses()


def test_dashboard_gone_is_already_gone(stub_factory, fake_clients):
    cloudwatch = stub_factory("cloudwatch", "us-east-1")
    cloudwatch.add_client_error(
        "list_tags_for_resource",
        service_error_code="ResourceNotFoundException",
        http_status_code=404,
        expected_params={"ResourceARN": DASHBOARD_ARN},
    )

    assert recheck(dashboard(), fake_clients) == "already-gone"
    cloudwatch.assert_no_pending_responses()


def monitor_response(tags: dict, status: str = "ACTIVE") -> dict:
    return {
        "MonitorName": "LAB-mon",
        "MonitorArn": f"arn:aws:internetmonitor:{REGION}:123456789012:monitor/LAB-mon",
        "Resources": [],
        "Status": status,
        "CreatedAt": days_ago(10),
        "ModifiedAt": days_ago(10),
        "Tags": tags,
    }


@pytest.mark.parametrize(
    ("tags", "reason"),
    [
        ({}, None),
        ({"janitor": "keep"}, "tagged janitor=keep"),
        ({"janitor": "later"}, None),
    ],
)
def test_monitor_keep_tag_from_get_monitor(stub_factory, fake_clients, tags, reason):
    monitor = stub_factory("internetmonitor", REGION)
    monitor.add_response("get_monitor", monitor_response(tags), {"MonitorName": "LAB-mon"})

    assert recheck(item("internet-monitor", "LAB-mon"), fake_clients) == reason
    monitor.assert_no_pending_responses()


def test_monitor_prefix_mismatch_and_gone(stub_factory, fake_clients):
    monitor = stub_factory("internetmonitor", REGION)
    monitor.add_client_error(
        "get_monitor",
        service_error_code="ResourceNotFoundException",
        http_status_code=404,
        expected_params={"MonitorName": "LAB-mon"},
    )

    assert recheck(item("internet-monitor", "prod-mon"), fake_clients) == "name prefix mismatch"
    assert recheck(item("internet-monitor", "LAB-mon"), fake_clients) == "already-gone"
    monitor.assert_no_pending_responses()


def test_snapshot_without_a_start_time_reads_age_unknown(stub_factory, fake_clients):
    described = snapshot()
    del described["StartTime"]
    ec2 = stub_factory("ec2", REGION)
    ec2.add_response("describe_snapshots", {"Snapshots": [described]}, {"SnapshotIds": ["snap-1"]})

    # Unknown is not "young": the reason must not read "below minimum age (None days)".
    assert recheck(item("ebs-snapshot", "snap-1"), fake_clients) == "age unknown"
    ec2.assert_no_pending_responses()


def test_volume_without_a_create_time_reads_age_unknown(stub_factory, fake_clients):
    ec2 = stub_factory("ec2", REGION)
    ec2.add_response(
        "describe_volumes",
        {"Volumes": [{"VolumeId": "vol-1", "State": "available", "Tags": LAB}]},
        {"VolumeIds": ["vol-1"]},
    )

    assert recheck(item("ebs-volume", "vol-1"), fake_clients) == "age unknown"
    ec2.assert_no_pending_responses()
