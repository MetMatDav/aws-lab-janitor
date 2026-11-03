"""Delete one plan item; the only module that calls a mutating AWS API.

Callers re-verify the item first. Internet Monitor monitors must be INACTIVE
before they can be deleted, so they are disabled and polled first.
"""

import time
from collections.abc import Callable

from executor.verify import client_for

MONITOR_POLL_SECONDS = 5
MONITOR_WAIT_SECONDS = 60
INACTIVE = "INACTIVE"

Sleep = Callable[[float], None]


class MonitorStillActive(Exception):
    """The monitor did not reach INACTIVE within ``MONITOR_WAIT_SECONDS``."""


def _snapshot(item: dict, clients, _sleep: Sleep) -> None:
    client_for(clients, "ec2", item).delete_snapshot(SnapshotId=item["resourceId"])


def _volume(item: dict, clients, _sleep: Sleep) -> None:
    client_for(clients, "ec2", item).delete_volume(VolumeId=item["resourceId"])


def _address(item: dict, clients, _sleep: Sleep) -> None:
    client_for(clients, "ec2", item).release_address(AllocationId=item["resourceId"])


def _dashboard(item: dict, clients, _sleep: Sleep) -> None:
    client_for(clients, "cloudwatch", item).delete_dashboards(DashboardNames=[item["resourceId"]])


def _monitor(item: dict, clients, sleep: Sleep) -> None:
    internetmonitor = client_for(clients, "internetmonitor", item)
    name = item["resourceId"]
    if internetmonitor.get_monitor(MonitorName=name)["Status"] != INACTIVE:
        internetmonitor.update_monitor(MonitorName=name, Status=INACTIVE)
        _wait_inactive(internetmonitor, name, sleep)
    internetmonitor.delete_monitor(MonitorName=name)


def _wait_inactive(internetmonitor, name: str, sleep: Sleep) -> None:
    """Poll every ``MONITOR_POLL_SECONDS`` until INACTIVE; raise after ``MONITOR_WAIT_SECONDS``."""
    waited = 0
    while waited < MONITOR_WAIT_SECONDS:
        sleep(MONITOR_POLL_SECONDS)
        waited += MONITOR_POLL_SECONDS
        if internetmonitor.get_monitor(MonitorName=name)["Status"] == INACTIVE:
            return
    raise MonitorStillActive(name)


DELETERS: dict[str, Callable[[dict, object, Sleep], None]] = {
    "ebs-snapshot": _snapshot,
    "ebs-volume": _volume,
    "elastic-ip": _address,
    "cw-dashboard": _dashboard,
    "internet-monitor": _monitor,
}


def delete(item: dict, clients, sleep: Sleep = time.sleep) -> None:
    """Delete ``item`` by its id with the API of its kind."""
    DELETERS[item["kind"]](item, clients, sleep)
