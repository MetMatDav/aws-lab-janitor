"""Re-verify one plan item against its live state just before it is deleted.

Each check reads that single resource by id (the executor never lists) and
mirrors the detector's rules. ``recheck`` returns None when the item is still
deletable, otherwise the reason to skip it; the first blocker wins. A
``ClientError`` the check does not expect propagates to the handler.
"""

from collections.abc import Callable
from datetime import datetime

from botocore.client import BaseClient
from botocore.exceptions import ClientError

from shared.marking import age_days, is_kept, is_lab_tagged
from shared.settings import Settings

GLOBAL_REGION = "global"
# Global kinds (CloudWatch dashboards) are served from us-east-1.
GLOBAL_CLIENT_REGION = "us-east-1"
ALREADY_GONE = "already-gone"
NOT_LAB_TAGGED = "not-lab-tagged"
NAME_PREFIX_MISMATCH = "name prefix mismatch"


def client_for(clients, service: str, item: dict) -> BaseClient:
    """The client of ``service`` in the item's region (us-east-1 for global items)."""
    region = item["region"]
    return clients.get(service, GLOBAL_CLIENT_REGION if region == GLOBAL_REGION else region)


def _error_code(error: ClientError) -> str:
    return error.response.get("Error", {}).get("Code", "")


def _describe_one(call: Callable[[], list[dict]], not_found_code: str) -> dict | None:
    """The single resource ``call`` returns, or None when it no longer exists."""
    try:
        resources = call()
    except ClientError as error:
        if _error_code(error) == not_found_code:
            return None
        raise
    return resources[0] if resources else None


def _keep_reason(settings: Settings) -> str:
    return f"tagged {settings.keep_tag_key}={settings.keep_tag_value}"


def _tag_blocker(tags: list[dict], settings: Settings) -> str | None:
    """``not-lab-tagged`` or the keep-tag reason, in that order."""
    if not is_lab_tagged(tags, settings):
        return NOT_LAB_TAGGED
    if is_kept(tags, settings):
        return _keep_reason(settings)
    return None


def _age_blocker(created: datetime | None, settings: Settings, now: datetime) -> str | None:
    age = age_days(created, now)
    if age is None or age < settings.min_age_days:
        return f"below minimum age ({age} days)"
    return None


def _snapshot(item: dict, clients, settings: Settings, now: datetime) -> str | None:
    ec2 = client_for(clients, "ec2", item)
    snapshot_id = item["resourceId"]
    snapshot = _describe_one(
        lambda: ec2.describe_snapshots(SnapshotIds=[snapshot_id])["Snapshots"],
        "InvalidSnapshot.NotFound",
    )
    if snapshot is None:
        return ALREADY_GONE
    blocker = _tag_blocker(snapshot.get("Tags", []), settings)
    if blocker:
        return blocker
    state = snapshot.get("State", "")
    if state != "completed":
        return f"state {state}"
    blocker = _age_blocker(snapshot.get("StartTime"), settings, now)
    if blocker:
        return blocker
    # Disabled AMIs are hidden by default but still pin their snapshots.
    images = ec2.describe_images(
        Owners=["self"],
        IncludeDisabled=True,
        Filters=[{"Name": "block-device-mapping.snapshot-id", "Values": [snapshot_id]}],
    )["Images"]
    if images:
        return f"backs AMI {images[0]['ImageId']}"
    return None


def _volume(item: dict, clients, settings: Settings, now: datetime) -> str | None:
    ec2 = client_for(clients, "ec2", item)
    volume = _describe_one(
        lambda: ec2.describe_volumes(VolumeIds=[item["resourceId"]])["Volumes"],
        "InvalidVolume.NotFound",
    )
    if volume is None:
        return ALREADY_GONE
    blocker = _tag_blocker(volume.get("Tags", []), settings)
    if blocker:
        return blocker
    if volume.get("Attachments"):
        return "attached"
    state = volume.get("State", "")
    if state != "available":
        return f"state {state}"
    return _age_blocker(volume.get("CreateTime"), settings, now)


def _address(item: dict, clients, settings: Settings, _now: datetime) -> str | None:
    ec2 = client_for(clients, "ec2", item)
    address = _describe_one(
        lambda: ec2.describe_addresses(AllocationIds=[item["resourceId"]])["Addresses"],
        "InvalidAllocationID.NotFound",
    )
    if address is None:
        return ALREADY_GONE
    blocker = _tag_blocker(address.get("Tags", []), settings)
    if blocker:
        return blocker
    if address.get("AssociationId"):
        return "associated"
    return None


def _dashboard(item: dict, clients, settings: Settings, _now: datetime) -> str | None:
    if not item["resourceId"].startswith(settings.name_prefix):
        return NAME_PREFIX_MISMATCH
    cloudwatch = client_for(clients, "cloudwatch", item)
    try:
        tags = cloudwatch.list_tags_for_resource(ResourceARN=item["arn"]).get("Tags", [])
    except ClientError as error:
        if _error_code(error) == "ResourceNotFoundException":
            return ALREADY_GONE
        raise
    return _keep_reason(settings) if is_kept(tags, settings) else None


def _monitor(item: dict, clients, settings: Settings, _now: datetime) -> str | None:
    name = item["resourceId"]
    if not name.startswith(settings.name_prefix):
        return NAME_PREFIX_MISMATCH
    internetmonitor = client_for(clients, "internetmonitor", item)
    try:
        monitor = internetmonitor.get_monitor(MonitorName=name)
    except ClientError as error:
        if _error_code(error) == "ResourceNotFoundException":
            return ALREADY_GONE
        raise
    tags = [{"Key": key, "Value": value} for key, value in monitor.get("Tags", {}).items()]
    return _keep_reason(settings) if is_kept(tags, settings) else None


RECHECKS: dict[str, Callable[[dict, object, Settings, datetime], str | None]] = {
    "ebs-snapshot": _snapshot,
    "ebs-volume": _volume,
    "elastic-ip": _address,
    "cw-dashboard": _dashboard,
    "internet-monitor": _monitor,
}


def recheck(item: dict, clients, settings: Settings, now: datetime) -> str | None:
    """None when ``item`` is still deletable, otherwise the reason to skip it."""
    return RECHECKS[item["kind"]](item, clients, settings, now)
