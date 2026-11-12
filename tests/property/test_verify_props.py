"""Property tests for ``executor/verify.py`` ``recheck`` on EBS volumes and snapshots.

Each example is one generated description of a live resource. The same
description feeds the executor's re-check and the detector's sweep
(``detector.detectors.ec2.detect_ec2``) through one in-memory EC2 fake, and an
independent oracle written from the rules lists what must block deletion.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from hypothesis import example, given
from hypothesis import strategies as st

from detector.detectors.ec2 import detect_ec2
from detector.sweep import SweepContext
from executor.verify import recheck
from shared.settings import Settings

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
REGION = "eu-west-1"
VOLUME_ID = "vol-0123456789abcdef0"
SNAPSHOT_ID = "snap-0123456789abcdef0"
TAG_KEY, TAG_VALUE = "Purpose", "lab"
KEEP_KEY, KEEP_VALUE = "janitor", "keep"


def make_settings(min_age_days: int) -> Settings:
    return Settings(
        dry_run=False,
        min_age_days=min_age_days,
        name_prefix="LAB-",
        tag_key=TAG_KEY,
        tag_value=TAG_VALUE,
        keep_tag_key=KEEP_KEY,
        keep_tag_value=KEEP_VALUE,
        plans_bucket="plans",
        audit_table="audit",
        reports_topic_arn="arn:aws:sns:eu-west-1:123456789012:reports",
        region=REGION,
    )


@dataclass
class Live:
    """One generated live resource and the settings it is judged under."""

    kind: str
    tags: list[dict]
    created: datetime | None
    min_age_days: int
    state: str
    attached: bool = False
    backs_ami: bool = False
    other_images: list[dict] = field(default_factory=list)

    def resource(self) -> dict:
        if self.kind == "ebs-volume":
            volume = {
                "VolumeId": VOLUME_ID,
                "State": self.state,
                "Size": 8,
                "VolumeType": "gp3",
                "Tags": self.tags,
                "Attachments": (
                    [{"InstanceId": "i-1", "VolumeId": VOLUME_ID, "State": "attached"}]
                    if self.attached
                    else []
                ),
            }
            if self.created is not None:
                volume["CreateTime"] = self.created
            return volume
        snapshot = {
            "SnapshotId": SNAPSHOT_ID,
            "State": self.state,
            "VolumeSize": 8,
            "VolumeId": VOLUME_ID,
            "Tags": self.tags,
        }
        if self.created is not None:
            snapshot["StartTime"] = self.created
        return snapshot

    def images(self) -> list[dict]:
        own = (
            [{"ImageId": "ami-1", "BlockDeviceMappings": [{"Ebs": {"SnapshotId": SNAPSHOT_ID}}]}]
            if self.backs_ami
            else []
        )
        return self.other_images + own


class FakeEc2:
    """Answers the EC2 reads of both the detector sweep and the executor re-check.

    Unknown operations raise AttributeError, so a new call cannot pass silently.
    """

    def __init__(self, live: Live) -> None:
        self.live = live

    @staticmethod
    def can_paginate(_operation: str) -> bool:
        return False

    def describe_volumes(self, **params) -> dict:
        found = [self.live.resource()] if self.live.kind == "ebs-volume" else []
        if "VolumeIds" in params:
            assert params == {"VolumeIds": [VOLUME_ID]}
        return {"Volumes": found}

    def describe_snapshots(self, **params) -> dict:
        found = [self.live.resource()] if self.live.kind == "ebs-snapshot" else []
        if "SnapshotIds" in params:
            assert params == {"SnapshotIds": [SNAPSHOT_ID]}
        else:
            assert params == {"OwnerIds": ["self"]}
        return {"Snapshots": found}

    def describe_images(self, **params) -> dict:
        assert params["Owners"] == ["self"]
        assert params["IncludeDisabled"] is True
        images = self.live.images()
        for flt in params.get("Filters", []):
            assert flt["Name"] == "block-device-mapping.snapshot-id"
            images = [
                image
                for image in images
                if any(
                    m.get("Ebs", {}).get("SnapshotId") in flt["Values"]
                    for m in image["BlockDeviceMappings"]
                )
            ]
        return {"Images": images}

    @staticmethod
    def describe_instances(**_params) -> dict:
        return {"Reservations": []}

    @staticmethod
    def describe_nat_gateways(**_params) -> dict:
        return {"NatGateways": []}

    @staticmethod
    def describe_addresses(**_params) -> dict:
        return {"Addresses": []}

    @staticmethod
    def describe_vpc_endpoints(**_params) -> dict:
        return {"VpcEndpoints": []}

    @staticmethod
    def describe_network_interfaces(**_params) -> dict:
        return {"NetworkInterfaces": []}


class Clients:
    def __init__(self, live: Live) -> None:
        self.live = live

    def get(self, service: str, region: str) -> FakeEc2:
        assert (service, region) == ("ec2", REGION)
        return FakeEc2(self.live)


# --- Strategies ------------------------------------------------------------------------

LAB_KEYS = [TAG_KEY, "purpose", "PURPOSE", " Purpose", "Purpose ", "Purpose\t"]
LAB_VALUES = [TAG_VALUE, "Lab", "LAB", " lab", "lab ", "", "lab\n"]
KEEP_KEYS = [KEEP_KEY, "Janitor", " janitor", "janitor ", "JANITOR"]
KEEP_VALUES = [KEEP_VALUE, "Keep", "keep ", " keep", "KEEP", "true", ""]


def exact_or_near(options: list[str]) -> st.SearchStrategy[str]:
    """The exact string (first option) half the time, otherwise one of its near misses."""
    return st.just(options[0]) | st.sampled_from(options[1:])


@st.composite
def tag_lists(draw, vary_lab: bool, vary_keep: bool) -> list[dict]:
    """Tags with unique keys (as AWS enforces). Unvaried, the exact lab tag is present and
    the exact keep tag absent; varied, those slots get exact or near-miss keys and values."""
    tags: dict[str, str] = {}
    if vary_lab:
        for _ in range(draw(st.integers(0, 2))):
            tags[draw(exact_or_near(LAB_KEYS))] = draw(exact_or_near(LAB_VALUES))
    else:
        tags[TAG_KEY] = TAG_VALUE
    if vary_keep:
        for _ in range(draw(st.integers(0, 2))):
            tags[draw(exact_or_near(KEEP_KEYS))] = draw(exact_or_near(KEEP_VALUES))
    else:
        # Near misses of the keep tag, which must not block.
        for _ in range(draw(st.integers(0, 1))):
            tags[draw(st.sampled_from(KEEP_KEYS[1:]))] = draw(st.sampled_from(KEEP_VALUES))
    if draw(st.booleans()):
        tags["Name"] = draw(st.sampled_from(["LAB-disk", "disk", "lab"]))
    noise = draw(st.dictionaries(st.text(max_size=5), st.text(max_size=5), max_size=2))
    for key, value in noise.items():
        tags.setdefault(key, value)
    pairs = [{"Key": key, "Value": value} for key, value in tags.items()]
    return draw(st.permutations(pairs))


min_ages = st.sampled_from([0, 1, 7]) | st.integers(0, 3650)
small_deltas = st.sampled_from(
    [
        timedelta(0),
        timedelta(microseconds=1),
        timedelta(microseconds=-1),
        timedelta(seconds=1),
        timedelta(seconds=-1),
        timedelta(hours=23, minutes=59, seconds=59),
    ]
)


@st.composite
def creation_times(draw, min_age_days: int) -> datetime | None:
    """None, the exact age boundary give or take a tick, the future, or any age."""
    choice = draw(st.sampled_from(["none", "boundary", "future", "any"]))
    if choice == "none":
        return None
    if choice == "boundary":
        return NOW - timedelta(days=min_age_days) + draw(small_deltas)
    if choice == "future":
        return NOW + timedelta(seconds=draw(st.integers(1, 10 * 86400)))
    return NOW - timedelta(seconds=draw(st.integers(-86400, 4000 * 86400)))


DIMENSIONS = ("lab", "keep", "age", "state", "attachment")


@st.composite
def lives(draw, kind: str) -> Live:
    """A resource that passes every rule except in the dimensions chosen to vary, which
    take any value (possibly still a passing one). Several dimensions can vary at once."""
    vary = draw(st.sets(st.sampled_from(DIMENSIONS)))
    min_age = draw(min_ages)
    if "age" in vary:
        created = draw(creation_times(min_age))
    else:
        extra = draw(
            st.sampled_from([timedelta(0), timedelta(microseconds=1), timedelta(days=1)])
            | st.timedeltas(min_value=timedelta(0), max_value=timedelta(days=1000))
        )
        created = NOW - timedelta(days=min_age) - extra
    common = {
        "kind": kind,
        "tags": draw(tag_lists("lab" in vary, "keep" in vary)),
        "created": created,
        "min_age_days": min_age,
    }
    if kind == "ebs-volume":
        states = ["available", "in-use", "creating", "deleting", "error", "Available", ""]
        return Live(
            **common,
            state=draw(st.sampled_from(states)) if "state" in vary else "available",
            attached="attachment" in vary and draw(st.booleans()),
        )
    states = ["completed", "pending", "error", "recoverable", "Completed", ""]
    other = {"ImageId": "ami-2", "BlockDeviceMappings": [{"Ebs": {"SnapshotId": "snap-other"}}]}
    return Live(
        **common,
        state=draw(st.sampled_from(states)) if "state" in vary else "completed",
        backs_ami="attachment" in vary and draw(st.booleans()),
        other_images=draw(st.sampled_from([[], [other]])),
    )


resources = lives("ebs-volume") | lives("ebs-snapshot")


# --- Oracle and the two sides ----------------------------------------------------------


def blockers(live: Live) -> list[str]:
    """Every rule that forbids deleting ``live``, written from the rules, not from the code."""
    found = []
    pairs = {(tag["Key"], tag["Value"]) for tag in live.tags}
    if (TAG_KEY, TAG_VALUE) not in pairs:
        found.append("not lab-tagged")
    if (KEEP_KEY, KEEP_VALUE) in pairs:
        found.append("keep tag")
    if live.created is None:
        found.append("no creation time")
    elif NOW - live.created < timedelta(days=live.min_age_days):
        found.append("too young")
    if live.kind == "ebs-volume":
        if live.attached:
            found.append("attached")
        if live.state != "available":
            found.append("not available")
    else:
        if live.state != "completed":
            found.append("not completed")
        if live.backs_ami:
            found.append("backs an AMI")
    return found


def executor_says_deletable(live: Live) -> bool:
    item = {
        "region": REGION,
        "kind": live.kind,
        "resourceId": VOLUME_ID if live.kind == "ebs-volume" else SNAPSHOT_ID,
        "arn": None,
    }
    return recheck(item, Clients(live), make_settings(live.min_age_days), NOW) is None


def detector_says_candidate(live: Live) -> bool:
    ctx = SweepContext(
        settings=make_settings(live.min_age_days),
        clients=Clients(live),
        now=NOW,
        account="123456789012",
        was_eligible_before=lambda _resource_id: False,
        deadline=time.time() + 600,
        log=logging.LoggerAdapter(logging.getLogger("test.property"), {}),
    )
    findings, errors = [], []
    detect_ec2(REGION, ctx, findings, errors)
    assert errors == []
    resource_id = VOLUME_ID if live.kind == "ebs-volume" else SNAPSHOT_ID
    return any(
        f.candidate for f in findings if f.kind == live.kind and f.resource_id == resource_id
    )


DELETABLE_VOLUME = Live(
    kind="ebs-volume",
    tags=[{"Key": TAG_KEY, "Value": TAG_VALUE}],
    created=NOW - timedelta(days=7),
    min_age_days=7,
    state="available",
)


@given(resources)
@example(DELETABLE_VOLUME)
@example(
    Live(
        kind="ebs-volume",
        tags=[{"Key": TAG_KEY, "Value": TAG_VALUE}, {"Key": KEEP_KEY, "Value": KEEP_VALUE}],
        created=NOW - timedelta(days=7) + timedelta(microseconds=1),
        min_age_days=7,
        state="in-use",
        attached=True,
    )
)
@example(
    Live(
        kind="ebs-snapshot",
        tags=[{"Key": " Purpose", "Value": TAG_VALUE}],
        created=NOW + timedelta(days=1),
        min_age_days=0,
        state="completed",
        backs_ami=True,
    )
)
def test_recheck_never_clears_a_blocked_resource(live):
    """Fails if recheck drops or loosens any rule: the keep tag, the exact lab tag, the
    minimum age (or an unknown age), attachment/state for volumes, state/AMI for snapshots.
    Also fails if it starts refusing a resource that no rule blocks."""
    reasons = blockers(live)
    deletable = executor_says_deletable(live)
    if reasons:
        assert not deletable, reasons
    else:
        assert deletable


@given(resources)
@example(DELETABLE_VOLUME)
def test_detector_and_recheck_agree(live):
    """Fails if the detector's candidate rules (ec2._volume / ec2._snapshot and the sweep's
    attachment and AMI-map handling) drift from the executor's re-check for the same state."""
    assert detector_says_candidate(live) == executor_says_deletable(live)
