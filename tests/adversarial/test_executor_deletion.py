"""Surface 1: getting something deleted that should not be.

Every test drives the real ``executor.handler``, ``executor.verify`` and
``executor.deleters`` against in-memory AWS fakes. Footholds: (b) can create or
tag resources but cannot approve, (c) can start executions or write the plans
bucket. A test that needs a forged plan says so: the plan is protected by its
sha256 and exact S3 version, which other tests here pin.
"""

import io
from datetime import UTC, datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody

from executor import handler as executor_handler
from executor import verify
from shared.plan import plan_bytes, plan_sha256
from shared.plan_store import PlanIntegrityError, PlanStore
from tests.adversarial.helpers import (
    ACCOUNT,
    KEEP,
    LAB,
    NOW,
    RUN_ID,
    VERSION,
    Executor,
    FakeAws,
    FakeContext,
    NoClients,
    approved_record,
    executor_event,
    outcomes,
    plan_item,
    settings,
)

DASHBOARD_ARN = f"arn:aws:cloudwatch::{ACCOUNT}:dashboard/LAB-dash"
MONITOR_ARN = f"arn:aws:internetmonitor:eu-west-1:{ACCOUNT}:monitor/LAB-mon"


def old(days: int = 42) -> datetime:
    """A creation time ``days`` before the real clock the handler reads."""
    return datetime.now(UTC) - timedelta(days=days)


def client_error(code: str, operation: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "scripted"}}, operation)


def snapshot(tags=LAB, **extra) -> dict:
    return {"SnapshotId": "snap-1", "State": "completed", "StartTime": old(), "Tags": tags, **extra}


def volume(tags=LAB, **extra) -> dict:
    return {"VolumeId": "vol-1", "State": "available", "CreateTime": old(), "Tags": tags, **extra}


def address(tags=LAB, **extra) -> dict:
    return {"AllocationId": "eipalloc-1", "PublicIp": "198.51.100.7", "Tags": tags, **extra}


def monitor(tags: dict | None = None, status: str = "INACTIVE") -> dict:
    return {"MonitorName": "LAB-mon", "MonitorArn": MONITOR_ARN, "Status": status, "Tags": tags}


ITEMS = {
    "ebs-snapshot": plan_item("ebs-snapshot", "snap-1"),
    "ebs-volume": plan_item("ebs-volume", "vol-1"),
    "elastic-ip": plan_item("elastic-ip", "eipalloc-1"),
    "cw-dashboard": plan_item("cw-dashboard", "LAB-dash", region="global", arn=DASHBOARD_ARN),
    "internet-monitor": plan_item("internet-monitor", "LAB-mon", arn=MONITOR_ARN),
}
DELETE_OPS = {
    "ebs-snapshot": "delete_snapshot",
    "ebs-volume": "delete_volume",
    "elastic-ip": "release_address",
    "cw-dashboard": "delete_dashboards",
    "internet-monitor": "delete_monitor",
}


def live_state(kind: str, *, kept: bool) -> dict:
    """Scripted read answers for one live, deletable resource of ``kind``."""
    ec2_tags = KEEP if kept else LAB
    named_tags = [{"Key": "janitor", "Value": "keep"}] if kept else []
    return {
        "ebs-snapshot": {
            "describe_snapshots": {"Snapshots": [snapshot(ec2_tags)]},
            "describe_images": {"Images": []},
        },
        "ebs-volume": {"describe_volumes": {"Volumes": [volume(ec2_tags)]}},
        "elastic-ip": {"describe_addresses": {"Addresses": [address(ec2_tags)]}},
        "cw-dashboard": {"list_tags_for_resource": {"Tags": named_tags}},
        "internet-monitor": {"get_monitor": monitor({"janitor": "keep"} if kept else {})},
    }[kind]


# --- the baseline: without it, "nothing was deleted" below would prove nothing ---------------


@pytest.mark.parametrize("kind", sorted(ITEMS))
def test_baseline_an_unchanged_candidate_is_deleted(monkeypatch, kind):
    aws = FakeAws(**live_state(kind, kept=False), **{DELETE_OPS[kind]: {}})
    executor = Executor(monkeypatch, [ITEMS[kind]], aws)

    result = executor.run()

    assert result["deleted"] == 1
    assert [call[2] for call in aws.mutations] == [DELETE_OPS[kind]]


# --- retagging after the plan ----------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(ITEMS))
def test_keep_tag_added_after_the_plan_stops_the_delete_of_every_kind(monkeypatch, kind):
    aws = FakeAws(**live_state(kind, kept=True))
    executor = Executor(monkeypatch, [ITEMS[kind]], aws)

    result = executor.run()

    assert outcomes(result) == [(ITEMS[kind]["resourceId"], "SKIPPED", "tagged janitor=keep")]
    assert aws.mutations == []


@pytest.mark.parametrize(
    "tags",
    [
        [{"Key": "purpose", "Value": "lab"}],
        [{"Key": "PURPOSE", "Value": "lab"}],
        [{"Key": "Purpose", "Value": "Lab"}],
        [{"Key": "Purpose", "Value": "LAB"}],
        [{"Key": "Purpose", "Value": "lab "}],
        [{"Key": "Purpose", "Value": " lab"}],
        [{"Key": "Purpose ", "Value": "lab"}],
        [{"Key": "Purpose", "Value": "lab\n"}],
        [{"Key": "Purpose", "Value": "lab\u200b"}],
        [{"Key": "Purpose", "Value": "la\u0431"}],
        [{"Key": "\u0420urpose", "Value": "lab"}],
        [{"Key": "Purpose", "Value": ""}],
        [{"Key": "Purpose"}],
        [{"Key": "Name", "Value": "LAB-looks-like-lab"}],
        [{"Key": "aws:Purpose", "Value": "lab"}],
    ],
    ids=repr,
)
@pytest.mark.parametrize("kind", ["ebs-snapshot", "ebs-volume", "elastic-ip"])
def test_lab_tag_lookalikes_after_the_plan_are_not_lab(monkeypatch, kind, tags):
    """Case, whitespace, homoglyph and name-prefix tricks: only ``Purpose=lab`` deletes EC2."""
    state = live_state(kind, kept=False)
    (described,) = next(iter(state.values())).values()
    described[0]["Tags"] = tags
    aws = FakeAws(**state)
    executor = Executor(monkeypatch, [ITEMS[kind]], aws)

    result = executor.run()

    assert outcomes(result) == [(ITEMS[kind]["resourceId"], "SKIPPED", "not-lab-tagged")]
    assert aws.mutations == []


def test_lab_tag_removed_and_keep_tag_added_reports_the_missing_lab_tag_first(monkeypatch):
    aws = FakeAws(describe_volumes={"Volumes": [volume([{"Key": "janitor", "Value": "keep"}])]})
    executor = Executor(monkeypatch, [ITEMS["ebs-volume"]], aws)

    assert outcomes(executor.run()) == [("vol-1", "SKIPPED", "not-lab-tagged")]
    assert aws.mutations == []


# --- state changes after the plan ------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "state", "reason"),
    [
        (
            "ebs-volume",
            {"describe_volumes": {"Volumes": [volume(Attachments=[{"InstanceId": "i-1"}])]}},
            "attached",
        ),
        (
            "ebs-volume",
            {"describe_volumes": {"Volumes": [volume(State="in-use")]}},
            "state in-use",
        ),
        ("ebs-volume", {"describe_volumes": {"Volumes": []}}, "already-gone"),
        ("ebs-snapshot", {"describe_snapshots": {"Snapshots": []}}, "already-gone"),
        ("elastic-ip", {"describe_addresses": {"Addresses": []}}, "already-gone"),
        (
            "elastic-ip",
            {"describe_addresses": {"Addresses": [address(AssociationId="eipassoc-1")]}},
            "associated",
        ),
        (
            "ebs-snapshot",
            {
                "describe_snapshots": {"Snapshots": [snapshot()]},
                "describe_images": {"Images": [{"ImageId": "ami-1"}]},
            },
            "backs AMI ami-1",
        ),
    ],
)
def test_resource_that_changed_after_the_plan_is_skipped(monkeypatch, kind, state, reason):
    aws = FakeAws(**state)
    executor = Executor(monkeypatch, [ITEMS[kind]], aws)

    assert outcomes(executor.run()) == [(ITEMS[kind]["resourceId"], "SKIPPED", reason)]
    assert aws.mutations == []


# --- verification that cannot be completed is never "safe to delete" -------------------------


@pytest.mark.parametrize(
    ("kind", "state"),
    [
        ("ebs-snapshot", {"describe_snapshots": client_error("RequestLimitExceeded")}),
        (
            "ebs-snapshot",
            {
                "describe_snapshots": {"Snapshots": [snapshot()]},
                "describe_images": client_error("UnauthorizedOperation"),
            },
        ),
        (
            "ebs-snapshot",
            {
                "describe_snapshots": {"Snapshots": [snapshot()]},
                "describe_images": client_error("RequestLimitExceeded"),
            },
        ),
        ("ebs-volume", {"describe_volumes": client_error("InternalError")}),
        # A resource id of another kind: EC2 answers Malformed, not NotFound.
        ("ebs-volume", {"describe_volumes": client_error("InvalidParameterValue")}),
        ("ebs-volume", {"describe_volumes": client_error("InvalidVolumeID.Malformed")}),
        # Near misses of the not-found code must not read as "already gone" either way.
        ("ebs-volume", {"describe_volumes": client_error("invalidvolume.notfound")}),
        ("ebs-volume", {"describe_volumes": client_error("InvalidSnapshot.NotFound")}),
        ("elastic-ip", {"describe_addresses": client_error("AuthFailure")}),
        ("cw-dashboard", {"list_tags_for_resource": client_error("AccessDenied")}),
        ("cw-dashboard", {"list_tags_for_resource": client_error("Throttling")}),
        ("internet-monitor", {"get_monitor": client_error("ThrottlingException")}),
        ("internet-monitor", {"get_monitor": client_error("AccessDeniedException")}),
    ],
)
def test_api_error_during_verification_fails_the_item_and_deletes_nothing(monkeypatch, kind, state):
    aws = FakeAws(**state)
    executor = Executor(monkeypatch, [ITEMS[kind]], aws)
    expected = next(v for v in state.values() if isinstance(v, ClientError))

    result = executor.run()

    assert outcomes(result) == [
        (ITEMS[kind]["resourceId"], "FAILED", expected.response["Error"]["Code"])
    ]
    assert aws.mutations == []


@pytest.mark.xfail(
    strict=True,
    reason="latent: a describe_images answer carrying NextToken is treated as the complete "
    "answer, so the snapshot is deleted without reading the remaining pages",
)
def test_truncated_ami_lookup_is_not_treated_as_no_ami(monkeypatch):
    """Foothold: none known. EC2 does not paginate DescribeImages unless MaxResults is sent,
    so this needs an API behaviour change; the check simply never looks at NextToken."""
    aws = FakeAws(
        describe_snapshots={"Snapshots": [snapshot()]},
        describe_images={"Images": [], "NextToken": "more-pages"},
        delete_snapshot={},
    )
    executor = Executor(monkeypatch, [ITEMS["ebs-snapshot"]], aws)

    result = executor.run()

    assert aws.mutations == []
    assert result["deleted"] == 0


# --- time and age arithmetic -----------------------------------------------------------------


def _age_reason(created, *, min_age_days: int = 7, now: datetime = NOW) -> str | None:
    aws = FakeAws(describe_volumes={"Volumes": [volume(CreateTime=created)]})
    return verify.recheck(ITEMS["ebs-volume"], aws, settings(minAgeDays=min_age_days), now)


@pytest.mark.parametrize(
    ("created", "min_age_days", "reason"),
    [
        (NOW - timedelta(days=7), 7, None),
        (NOW - timedelta(days=7) + timedelta(seconds=1), 7, "below minimum age (6 days)"),
        (
            NOW - timedelta(days=6, hours=23, minutes=59, seconds=59),
            7,
            "below minimum age (6 days)",
        ),
        # Clock skew or a forged timestamp: a creation date in the future is never old enough,
        # not even when the minimum age is zero.
        (NOW + timedelta(seconds=1), 0, "below minimum age (-1 days)"),
        (NOW + timedelta(days=3650), 7, "below minimum age (-3650 days)"),
        (NOW + timedelta(days=3650), 0, "below minimum age (-3650 days)"),
        (NOW, 0, None),
        (None, 0, "age unknown"),
        # The same instant written in another zone is the same age.
        ((NOW - timedelta(days=7)).astimezone(timezone(timedelta(hours=-5))), 7, None),
        ((NOW - timedelta(days=7)).astimezone(timezone(timedelta(hours=14))), 7, None),
    ],
)
def test_age_boundaries_future_dates_and_clock_skew(created, min_age_days, reason):
    assert _age_reason(created, min_age_days=min_age_days) == reason


@pytest.mark.parametrize(
    "created",
    [
        (NOW - timedelta(days=4000)).replace(tzinfo=None),
        (NOW - timedelta(days=4000)).isoformat(),
        (NOW - timedelta(days=4000)).timestamp(),
        0,
        "",
        True,
    ],
    ids=["naive-datetime", "iso-string", "epoch-float", "zero", "empty-string", "bool"],
)
def test_creation_date_of_the_wrong_type_never_reads_as_old_enough(monkeypatch, created):
    """A timezone-naive or non-datetime timestamp must fail closed, never compare as ancient."""
    aws = FakeAws(describe_volumes={"Volumes": [volume(CreateTime=created)]})
    executor = Executor(monkeypatch, [ITEMS["ebs-volume"]], aws)

    try:
        result = executor.run()
    except TypeError:
        result = None

    assert aws.mutations == []
    if result is not None:
        assert result["deleted"] == 0


# --- the arming switches ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "True",
        "TRUE",
        "1",
        " true",
        "true ",
        "true\n",
        "\ttrue",
        "yes",
        "on",
        "enabled",
        "tru\u0435",
    ],
    ids=repr,
)
def test_deletions_enabled_variants_do_not_arm_the_executor(monkeypatch, raw):
    executor = Executor(monkeypatch, [ITEMS["ebs-volume"]])
    monkeypatch.setenv("DELETIONS_ENABLED", raw)

    assert outcomes(executor.run()) == [("vol-1", "SKIPPED", "deletions-disabled")]
    assert executor.clients.requested == []


@pytest.mark.parametrize("dry_run", ["False", "FALSE", "0", 0, 0.0, [], {}, "", "no"], ids=repr)
def test_only_the_boolean_false_disables_dry_run(monkeypatch, dry_run):
    """``0 == False`` in Python: the check must be identity, or ``dryRun: 0`` would arm it."""
    executor = Executor(monkeypatch, [ITEMS["ebs-volume"]])

    result = executor.run(executor_event(dry_run=dry_run))

    assert outcomes(result) == [("vol-1", "SKIPPED", "deletions-disabled")]
    assert executor.clients.requested == []


@pytest.mark.parametrize(
    "approval",
    [
        None,
        {},
        {"decision": "Approved"},
        {"decision": "approve"},
        {"decision": "approved "},
        {"decision": True},
        {"decision": ["approved"]},
        {"Decision": "approved"},
        {"decision": "rejected"},
    ],
    ids=repr,
)
def test_the_callback_output_must_say_exactly_approved(monkeypatch, approval):
    executor = Executor(monkeypatch, [ITEMS["ebs-volume"]])
    event = executor_event()
    event["approval"] = approval

    assert outcomes(executor.run(event)) == [("vol-1", "SKIPPED", "deletions-disabled")]
    assert executor.clients.requested == []


# --- the plan itself -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        "ebs-volume ",
        " ebs-volume",
        "EBS-VOLUME",
        "ebs_volume",
        "ebs-volume\n",
        "ec2-instance",
        "s3-bucket",
        "rds-instance",
        "nat-gateway",
        "ami",
        "",
        None,
        0,
        ["ebs-volume"],
        {"ebs-volume": True},
    ],
    ids=repr,
)
def test_kind_that_is_not_exactly_deletable_never_reaches_aws(monkeypatch, kind):
    executor = Executor(monkeypatch, [plan_item(kind, "vol-1")])

    result = executor.run()

    assert outcomes(result) == [("vol-1", "SKIPPED", "no-deleter")]
    assert executor.clients.requested == []


def test_a_named_kind_without_the_lab_prefix_is_refused_before_any_call(monkeypatch):
    """A plan item naming a production dashboard or monitor: the prefix is re-checked."""
    items = [
        plan_item("cw-dashboard", "prod-dash", region="global", arn=DASHBOARD_ARN),
        plan_item("cw-dashboard", "lab-dash", region="global", arn=DASHBOARD_ARN),
        plan_item("cw-dashboard", " LAB-dash", region="global", arn=DASHBOARD_ARN),
        plan_item("internet-monitor", "xLAB-mon"),
        plan_item("internet-monitor", "LAB"),
    ]
    aws = FakeAws()
    executor = Executor(monkeypatch, items, aws)

    result = executor.run()

    assert {reason for _id, _outcome, reason in outcomes(result)} == {"name prefix mismatch"}
    assert aws.calls == []


def _s3_body(data: bytes) -> dict:
    return {"Body": StreamingBody(io.BytesIO(data), len(data))}


def test_plan_with_an_appended_item_is_refused_and_only_the_pinned_version_is_read(
    monkeypatch, s3_stub
):
    """Foothold (c): a writer of the plans bucket adds a version with one more candidate.

    The executor reads the exact version Detect reported (the Stubber checks ``VersionId``)
    and, even if those bytes were swapped, refuses them because the sha256 differs.
    """
    s3, stubber = s3_stub
    approved = {"runId": RUN_ID, "candidates": [ITEMS["ebs-volume"]]}
    forged = {
        "runId": RUN_ID,
        "candidates": [ITEMS["ebs-volume"], plan_item("ebs-volume", "vol-9")],
    }
    sha = plan_sha256(plan_bytes(approved))
    stubber.add_response(
        "get_object",
        _s3_body(plan_bytes(forged)),
        {"Bucket": "test-plans", "Key": f"plans/{RUN_ID}/plan.json", "VersionId": VERSION},
    )
    executor = Executor(monkeypatch, [])
    executor.store = PlanStore(s3, "test-plans")
    executor.approvals.record = approved_record(plan_sha256=sha)
    event = executor_event()
    event["detect"]["planSha256"] = sha

    with pytest.raises(PlanIntegrityError, match="plan sha256 differs"):
        executor.run(event)

    stubber.assert_no_pending_responses()
    assert executor.clients.requested == []
    assert executor.audit.events == []


@pytest.mark.parametrize(
    "whitespace_variant",
    [
        lambda data: data + b"\n",
        lambda data: data.replace(b",", b", ", 1),
        lambda data: b"\xef\xbb\xbf" + data,
    ],
    ids=["trailing-newline", "extra-space", "bom"],
)
def test_same_plan_in_other_bytes_is_refused(monkeypatch, s3_stub, whitespace_variant):
    """The digest is over the stored bytes, not over a re-serialization of the parsed plan."""
    s3, stubber = s3_stub
    data = plan_bytes({"runId": RUN_ID, "candidates": [ITEMS["ebs-volume"]]})
    sha = plan_sha256(data)
    stubber.add_response("get_object", _s3_body(whitespace_variant(data)))
    executor = Executor(monkeypatch, [])
    executor.store = PlanStore(s3, "test-plans")
    executor.approvals.record = approved_record(plan_sha256=sha)
    event = executor_event()
    event["detect"]["planSha256"] = sha

    with pytest.raises((PlanIntegrityError, ValueError)):
        executor.run(event)

    assert executor.clients.requested == []


def test_integrity_message_names_every_disagreement_but_no_secret(monkeypatch):
    executor = Executor(monkeypatch, [ITEMS["ebs-volume"]])
    executor.approvals.record = approved_record(
        run_id="20261026T120000Z-ffff", plan_sha256="b" * 64, status="PENDING"
    )

    with pytest.raises(PlanIntegrityError) as raised:
        executor.run()

    message = str(raised.value)
    assert "approval belongs to another run" in message
    assert "plan sha256 differs from the approval" in message
    assert "approval is not APPROVED" in message
    assert executor.approvals.record.approval_id not in message
    assert "task-token" not in message


@pytest.mark.xfail(
    strict=True,
    reason="an APPROVED record never goes stale: invoking Apply again weeks later (redrive, "
    "direct invoke) re-applies the old plan to whatever passes the live checks by then",
)
def test_apply_replayed_long_after_the_approval_is_refused(monkeypatch):
    """Foothold (c) plus lambda:InvokeFunction or states:RedriveExecution.

    Everything in the payload is public inside the account (audit table, execution
    history). Items SKIPPED in the first apply (too young, attached, kept at the time)
    are deleted by the replay once they pass the live checks, with no new approval.
    """
    aws = FakeAws(describe_volumes={"Volumes": [volume()]}, delete_volume={})
    executor = Executor(monkeypatch, [ITEMS["ebs-volume"]], aws)
    decided = datetime.now(UTC) - timedelta(days=25)
    executor.approvals.record = approved_record(
        created_at=(decided - timedelta(hours=1)).isoformat(),
        expires_at=(decided + timedelta(hours=70)).isoformat(),
        decided_at=decided.isoformat(),
    )

    try:
        result = executor.run()
    except PlanIntegrityError:
        result = {"deleted": 0}

    assert aws.mutations == []
    assert result["deleted"] == 0


@pytest.mark.xfail(
    strict=True,
    reason="latent: the dashboard keep-tag check reads the plan item's arn but the delete uses "
    "its resourceId; when they name different dashboards the kept one is deleted",
)
def test_dashboard_keep_tag_is_read_from_the_dashboard_that_gets_deleted():
    """Foothold: a forged plan, which the sha256 check stops today (defence in depth).

    The detector always writes a matching arn; the executor trusts that instead of
    deriving the ARN from the name it is about to delete.
    """
    decoy_arn = f"arn:aws:cloudwatch::{ACCOUNT}:dashboard/LAB-decoy"
    kept_arn = f"arn:aws:cloudwatch::{ACCOUNT}:dashboard/LAB-kept"

    def tags(ResourceARN: str) -> dict:
        kept = ResourceARN == kept_arn
        return {"Tags": [{"Key": "janitor", "Value": "keep"}] if kept else []}

    aws = FakeAws(list_tags_for_resource=tags)
    item = plan_item("cw-dashboard", "LAB-kept", region="global", arn=decoy_arn)

    assert verify.recheck(item, aws, settings(), NOW) is not None


def test_recheck_and_delete_use_the_region_of_the_item_and_no_other(monkeypatch):
    aws = FakeAws(describe_volumes={"Volumes": [volume()]}, delete_volume={})
    executor = Executor(monkeypatch, [plan_item("ebs-volume", "vol-1", region="ap-south-1")], aws)

    executor.run()

    assert {(service, region) for service, region, _op, _params in aws.calls} == {
        ("ec2", "ap-south-1")
    }


def test_delete_targets_exactly_the_id_that_was_verified(monkeypatch):
    aws = FakeAws(
        describe_snapshots={"Snapshots": [snapshot()]},
        describe_images={"Images": []},
        delete_snapshot={},
    )
    executor = Executor(monkeypatch, [ITEMS["ebs-snapshot"]], aws)

    executor.run()

    by_op = {op: params for _service, _region, op, params in aws.calls}
    assert by_op["describe_snapshots"] == {"SnapshotIds": ["snap-1"]}
    assert by_op["delete_snapshot"] == {"SnapshotId": "snap-1"}
    assert len(aws.mutations) == 1


def test_time_budget_is_checked_before_the_item_not_only_at_the_start(monkeypatch):
    """A context that runs out between items: the second item is skipped, not started."""
    aws = FakeAws(describe_volumes={"Volumes": [volume()]}, delete_volume={})
    items = [ITEMS["ebs-volume"], plan_item("ebs-volume", "vol-2")]
    executor = Executor(monkeypatch, items, aws)
    context = FakeContext()
    original = aws.answers["delete_volume"]

    def delete_and_burn_the_clock(**_params):
        context.remaining_ms = executor_handler.RESERVE_SECONDS * 1000 - 1
        return original

    aws.answers["delete_volume"] = delete_and_burn_the_clock

    result = executor.run(context=context)

    assert outcomes(result) == [
        ("vol-1", "DELETED", "planned vol-1"),
        ("vol-2", "SKIPPED", "time-budget"),
    ]
    assert len(aws.mutations) == 1


def test_no_clients_are_created_when_nothing_is_armed(monkeypatch):
    executor = Executor(monkeypatch, list(ITEMS.values()))
    monkeypatch.delenv("DELETIONS_ENABLED")

    result = executor.run()

    assert result["skipped"] == len(ITEMS)
    assert isinstance(executor.clients, NoClients)
    assert executor.clients.requested == []
