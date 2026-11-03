import inspect
import json
from datetime import datetime

import pytest

from executor import deleters, verify
from executor import handler as handler_module
from shared.approvals import APPROVED, DECIDING, ApprovalRecord
from shared.plan_store import PlanIntegrityError
from tests.unit.conftest import days_ago

RUN_ID = "20261102T120000Z-abcd"
APPROVAL_ID = "approval-1"
SHA = "a" * 64
VERSION = "pv-1"
LAB = [{"Key": "Purpose", "Value": "lab"}]


def candidate(resource_id: str, kind: str = "ebs-snapshot", region: str = "eu-west-1") -> dict:
    return {
        "region": region,
        "kind": kind,
        "resourceId": resource_id,
        "arn": None,
        "detail": "",
        "lab": True,
        "candidate": True,
        "eligible": False,
        "reason": f"planned {resource_id}",
        "ageDays": 42,
        "estimatedMonthlyUsd": 0.4,
    }


def record(**overrides) -> ApprovalRecord:
    values = {
        "approval_id": APPROVAL_ID,
        "run_id": RUN_ID,
        "plan_version_id": VERSION,
        "plan_sha256": SHA,
        "plan_summary": {},
        "status": APPROVED,
        "created_at": "2026-11-02T12:00:00+00:00",
        "expires_at": "2026-11-05T11:30:00+00:00",
        "approved_by": {"mode": "link"},
        "decided_at": "2026-11-02T13:00:00+00:00",
    }
    values.update(overrides)
    return ApprovalRecord(**values)


class FakeStore:
    def __init__(self, candidates: list[dict], sha: str = SHA) -> None:
        self.plan = {"runId": RUN_ID, "candidates": candidates}
        self.sha = sha
        self.reads: list[tuple[str, str]] = []

    def get_plan(self, run_id: str, version_id: str) -> tuple[dict, str]:
        self.reads.append((run_id, version_id))
        return self.plan, self.sha


class FakeApprovals:
    def __init__(self, approval: ApprovalRecord | None) -> None:
        self.approval = approval
        self.reads: list[str] = []

    def get(self, approval_id: str):
        self.reads.append(approval_id)
        return None if self.approval is None else (self.approval, "secret-task-token")


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def put_run_event(  # noqa: PLR0913, PLR0917
        self, run_id, region, resource_id, kind, event, actor, dry_run, detail, at
    ) -> None:
        self.events.append(
            {
                "run_id": run_id,
                "region": region,
                "resource_id": resource_id,
                "kind": kind,
                "event": event,
                "actor": actor,
                "dry_run": dry_run,
                "detail": detail,
                "at": at,
            }
        )

    def put_run_summary(self, *args, **kwargs) -> None:
        raise AssertionError("the executor must not write a run summary")


class FakeEvents:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    def put_events(self, Entries: list[dict]) -> dict:
        self.entries.extend(Entries)
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "e"} for _ in Entries]}


class FakeContext:
    def __init__(self, remaining_ms: int = 600_000) -> None:
        self.remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self.remaining_ms


class NoClients:
    def get(self, service: str, region: str):
        raise AssertionError(f"unexpected client {service} in {region}")


class Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, candidates: list[dict]) -> None:
        self.monkeypatch = monkeypatch
        self.clients = NoClients()
        self.store = FakeStore(candidates)
        self.approvals = FakeApprovals(record())
        self.audit = FakeAudit()
        self.events = FakeEvents()
        self.rechecks: list[str] = []
        self.deletes: list[str] = []
        self.recheck_reasons: dict[str, str] = {}
        self.delete_errors: dict[str, Exception] = {}

        monkeypatch.setenv("DELETIONS_ENABLED", "true")
        monkeypatch.setattr(
            handler_module,
            "build_dependencies",
            lambda settings: (self.clients, self.store, self.approvals, self.audit, self.events),
        )

    def fake_aws(self) -> None:
        """Replace re-verification and deletion with recording fakes."""

        def recheck(item, clients, settings, now):
            self.rechecks.append(item["resourceId"])
            return self.recheck_reasons.get(item["resourceId"])

        def delete(item, clients, sleep=None):
            self.deletes.append(item["resourceId"])
            error = self.delete_errors.get(item["resourceId"])
            if error is not None:
                raise error

        self.monkeypatch.setattr(verify, "recheck", recheck)
        self.monkeypatch.setattr(deleters, "delete", delete)


@pytest.fixture
def make_harness(monkeypatch):
    return lambda candidates: Harness(monkeypatch, candidates)


def event(dry_run=False, decision="approved", **extra) -> dict:
    return {
        "detect": {"runId": RUN_ID, "planVersionId": VERSION, "planSha256": SHA},
        "config": {"dryRun": dry_run, "minAgeDays": 7},
        "approval": {"decision": decision, "approvalId": APPROVAL_ID, "approvedBy": {}},
        "approvalId": APPROVAL_ID,
        **extra,
    }


def outcomes(result: dict) -> list[tuple[str, str, str]]:
    return [(i["resourceId"], i["outcome"], i["reason"]) for i in result["items"]]


def test_deletable_kinds_have_a_recheck_and_a_deleter():
    assert set(handler_module.DELETABLE_KINDS) == set(verify.RECHECKS) == set(deleters.DELETERS)


def test_everything_skipped_when_deletions_disabled(make_harness, monkeypatch):
    harness = make_harness([candidate("snap-1"), candidate("vol-1", kind="ebs-volume")])
    monkeypatch.delenv("DELETIONS_ENABLED")

    result = handler_module.handler(event(), FakeContext())

    assert outcomes(result) == [
        ("snap-1", "SKIPPED", "deletions-disabled"),
        ("vol-1", "SKIPPED", "deletions-disabled"),
    ]
    assert (result["deleted"], result["failed"], result["skipped"]) == (0, 0, 2)
    assert [e["event"] for e in harness.audit.events] == ["SKIPPED", "SKIPPED"]
    assert [e["DetailType"] for e in harness.events.entries] == ["janitor.resource.skipped"] * 2


@pytest.mark.parametrize(
    ("dry_run", "decision"),
    [("false", "approved"), (True, "approved"), (None, "approved"), (False, "rejected")],
)
def test_everything_skipped_when_dry_run_not_boolean_false(make_harness, dry_run, decision):
    harness = make_harness([candidate("snap-1")])

    result = handler_module.handler(event(dry_run=dry_run, decision=decision), FakeContext())

    assert outcomes(result) == [("snap-1", "SKIPPED", "deletions-disabled")]
    assert harness.audit.events[0]["dry_run"] is False


@pytest.mark.parametrize(
    ("store_sha", "detect_sha", "approval"),
    [
        ("b" * 64, SHA, record()),
        (SHA, "b" * 64, record()),
        (SHA, SHA, record(plan_sha256="b" * 64)),
        (SHA, SHA, record(run_id="20261026T120000Z-ffff")),
        (SHA, SHA, record(approval_id="approval-2")),
        (SHA, SHA, None),
    ],
)
def test_integrity_mismatch_raises_before_any_write(make_harness, store_sha, detect_sha, approval):
    harness = make_harness([candidate("snap-1")])
    harness.store.sha = store_sha
    harness.approvals.approval = approval
    payload = event()
    payload["detect"]["planSha256"] = detect_sha

    with pytest.raises(PlanIntegrityError):
        handler_module.handler(payload, FakeContext())

    assert harness.audit.events == []
    assert harness.events.entries == []


@pytest.mark.parametrize("status", [DECIDING, "PENDING", "REJECTED", "EXPIRED"])
def test_approval_not_approved_raises(make_harness, status):
    harness = make_harness([candidate("snap-1")])
    harness.approvals.approval = record(status=status)

    with pytest.raises(PlanIntegrityError):
        handler_module.handler(event(), FakeContext())

    assert harness.audit.events == []
    assert harness.events.entries == []


def test_deletes_only_plan_items_and_audits_each(make_harness):
    harness = make_harness([candidate("snap-1"), candidate("vol-1", kind="ebs-volume")])
    harness.fake_aws()
    harness.recheck_reasons = {"vol-1": "attached"}

    result = handler_module.handler(event(), FakeContext())

    assert harness.store.reads == [(RUN_ID, VERSION)]
    assert harness.approvals.reads == [APPROVAL_ID]
    assert harness.rechecks == ["snap-1", "vol-1"]
    assert harness.deletes == ["snap-1"]
    assert outcomes(result) == [
        ("snap-1", "DELETED", "planned snap-1"),
        ("vol-1", "SKIPPED", "attached"),
    ]
    assert result == {
        "runId": RUN_ID,
        "approvalId": APPROVAL_ID,
        "deleted": 1,
        "failed": 0,
        "skipped": 1,
        "items": [
            {
                "resourceId": "snap-1",
                "kind": "ebs-snapshot",
                "region": "eu-west-1",
                "outcome": "DELETED",
                "reason": "planned snap-1",
            },
            {
                "resourceId": "vol-1",
                "kind": "ebs-volume",
                "region": "eu-west-1",
                "outcome": "SKIPPED",
                "reason": "attached",
            },
        ],
    }
    first = harness.audit.events[0]
    assert {k: v for k, v in first.items() if k != "at"} == {
        "run_id": RUN_ID,
        "region": "eu-west-1",
        "resource_id": "snap-1",
        "kind": "ebs-snapshot",
        "event": "DELETED",
        "actor": "lambda:executor",
        "dry_run": False,
        "detail": {
            "reason": "planned snap-1",
            "approvalId": APPROVAL_ID,
            "planSha256": SHA,
            "estimatedMonthlyUsd": 0.4,
        },
    }
    assert isinstance(first["at"], datetime)
    assert [e["event"] for e in harness.audit.events] == ["DELETED", "SKIPPED"]

    entries = harness.events.entries
    assert [(e["Source"], e["DetailType"], e["EventBusName"]) for e in entries] == [
        ("lab-janitor", "janitor.resource.deleted", "default"),
        ("lab-janitor", "janitor.resource.skipped", "default"),
    ]
    detail = json.loads(entries[0]["Detail"])
    assert detail == {
        "runId": RUN_ID,
        "region": "eu-west-1",
        "resourceId": "snap-1",
        "kind": "ebs-snapshot",
        "event": "DELETED",
        "actor": "lambda:executor",
        "dryRun": False,
        "detail": first["detail"],
        "at": first["at"].isoformat(),
    }


def test_kind_without_deleter_is_skipped(make_harness):
    harness = make_harness([candidate("nat-1", kind="nat-gateway")])
    harness.fake_aws()

    result = handler_module.handler(event(), FakeContext())

    assert outcomes(result) == [("nat-1", "SKIPPED", "no-deleter")]
    assert harness.rechecks == []
    assert harness.deletes == []


def snapshot_in_plan_stubs(ec2) -> None:
    ec2.add_response(
        "describe_snapshots",
        {
            "Snapshots": [
                {
                    "SnapshotId": "snap-1",
                    "StartTime": days_ago(42),
                    "State": "completed",
                    "Tags": LAB,
                }
            ]
        },
        {"SnapshotIds": ["snap-1"]},
    )
    ec2.add_response(
        "describe_images",
        {"Images": []},
        {
            "Owners": ["self"],
            "IncludeDisabled": True,
            "Filters": [{"Name": "block-device-mapping.snapshot-id", "Values": ["snap-1"]}],
        },
    )


def test_failed_on_invalid_snapshot_in_use(make_harness, stub_factory, fake_clients):
    harness = make_harness([candidate("snap-1"), candidate("snap-2")])
    harness.clients = fake_clients
    ec2 = stub_factory("ec2", "eu-west-1")
    snapshot_in_plan_stubs(ec2)
    ec2.add_client_error(
        "delete_snapshot",
        service_error_code="InvalidSnapshot.InUse",
        http_status_code=400,
        expected_params={"SnapshotId": "snap-1"},
    )
    ec2.add_client_error(
        "describe_snapshots",
        service_error_code="UnauthorizedOperation",
        http_status_code=403,
        expected_params={"SnapshotIds": ["snap-2"]},
    )

    result = handler_module.handler(event(), FakeContext())

    assert outcomes(result) == [
        ("snap-1", "FAILED", "InvalidSnapshot.InUse"),
        ("snap-2", "FAILED", "UnauthorizedOperation"),
    ]
    assert result["failed"] == 2
    assert harness.audit.events[0]["detail"]["reason"] == "InvalidSnapshot.InUse"
    assert harness.events.entries[0]["DetailType"] == "janitor.resource.failed"
    ec2.assert_no_pending_responses()


def test_monitor_still_active_is_failed(make_harness):
    harness = make_harness([candidate("LAB-mon", kind="internet-monitor")])
    harness.fake_aws()
    harness.delete_errors = {"LAB-mon": deleters.MonitorStillActive("LAB-mon")}

    result = handler_module.handler(event(), FakeContext())

    assert outcomes(result) == [("LAB-mon", "FAILED", "monitor-still-active")]


def test_unexpected_exception_is_audited_then_raised(make_harness):
    harness = make_harness([candidate("snap-1"), candidate("snap-2")])
    harness.fake_aws()
    harness.delete_errors = {"snap-1": RuntimeError("boom")}

    with pytest.raises(RuntimeError, match="boom"):
        handler_module.handler(event(), FakeContext())

    assert [(e["resource_id"], e["event"]) for e in harness.audit.events] == [("snap-1", "FAILED")]
    assert harness.audit.events[0]["detail"]["reason"] == "exception"
    assert harness.deletes == ["snap-1"]


def test_time_budget_skips_the_rest(make_harness):
    harness = make_harness([candidate("snap-1"), candidate("vol-1", kind="ebs-volume")])
    harness.fake_aws()

    result = handler_module.handler(event(), FakeContext(remaining_ms=20_000))

    assert outcomes(result) == [
        ("snap-1", "SKIPPED", "time-budget"),
        ("vol-1", "SKIPPED", "time-budget"),
    ]
    assert harness.rechecks == []
    assert harness.deletes == []


def test_no_delete_call_for_a_resource_absent_from_the_plan(
    make_harness, stub_factory, fake_clients
):
    harness = make_harness([candidate("snap-1")])
    harness.clients = fake_clients
    ec2 = stub_factory("ec2", "eu-west-1")
    snapshot_in_plan_stubs(ec2)
    ec2.add_response("delete_snapshot", {}, {"SnapshotId": "snap-1"})
    payload = event(candidates=[candidate("snap-intruder")])
    payload["detect"]["candidates"] = [candidate("snap-intruder")]

    result = handler_module.handler(payload, FakeContext())

    assert outcomes(result) == [("snap-1", "DELETED", "planned snap-1")]
    assert [e["resource_id"] for e in harness.audit.events] == ["snap-1"]
    # Any extra call (such as deleting snap-intruder) would fail the Stubber.
    ec2.assert_no_pending_responses()


def test_result_items_are_bounded(make_harness, monkeypatch):
    harness = make_harness([candidate(f"snap-{n}") for n in range(250)])
    monkeypatch.delenv("DELETIONS_ENABLED")

    result = handler_module.handler(event(), FakeContext())

    assert len(result["items"]) == handler_module.MAX_RESULT_ITEMS == 200
    assert result["items"][-1]["resourceId"] == "snap-199"
    assert result["skipped"] == 250
    assert len(harness.audit.events) == len(harness.events.entries) == 250


def test_task_token_is_never_logged(make_harness, caplog):
    harness = make_harness([candidate("snap-1")])
    harness.fake_aws()

    with caplog.at_level("DEBUG"):
        handler_module.handler(event(), FakeContext())

    assert "secret-task-token" not in caplog.text
    assert APPROVAL_ID not in caplog.text
    assert any("snap-1" in r.getMessage() and "DELETED" in r.getMessage() for r in caplog.records)
    assert all(r.runId == RUN_ID for r in caplog.records if r.name == "executor")


def test_executor_has_no_main():
    for module in (handler_module, verify, deleters):
        assert "__main__" not in inspect.getsource(module)
