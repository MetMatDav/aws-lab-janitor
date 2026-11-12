"""In-memory fakes shared by the adversarial tests. Nothing here talks to AWS."""

import os
from datetime import UTC, datetime, timedelta

from executor import handler as executor_handler
from shared.approvals import APPROVED, ApprovalRecord
from shared.settings import Settings, load_settings

ACCOUNT = "123456789012"
RUN_ID = "20261102T120000Z-abc1"
APPROVAL_ID = "11111111-aaaa-bbbb-cccc-222222222222"
TASK_TOKEN = "task-token-not-a-real-one"  # noqa: S105 - test value
SHA = "a" * 64
VERSION = "plan-v1"
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
REGION = "eu-west-1"
LAB = [{"Key": "Purpose", "Value": "lab"}]
KEEP = [*LAB, {"Key": "janitor", "Value": "keep"}]


def days_ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


def settings(**config) -> Settings:
    return load_settings({"config": config}, os.environ)


def plan_item(kind: str, resource_id: str, region: str = REGION, **overrides) -> dict:
    item = {
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
    item.update(overrides)
    return item


def approved_record(**overrides) -> ApprovalRecord:
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
        # Fresh: the executor refuses an approval decided more than an hour ago.
        "decided_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    }
    values.update(overrides)
    return ApprovalRecord(**values)


def executor_event(dry_run=False, decision="approved", min_age_days=7) -> dict:
    return {
        "detect": {"runId": RUN_ID, "planVersionId": VERSION, "planSha256": SHA},
        "config": {"dryRun": dry_run, "minAgeDays": min_age_days},
        "approval": {"decision": decision, "approvalId": APPROVAL_ID, "approvedBy": {}},
        "approvalId": APPROVAL_ID,
    }


class FakePlanStore:
    def __init__(self, candidates: list[dict], sha: str = SHA) -> None:
        self.plan = {"runId": RUN_ID, "candidates": candidates}
        self.sha = sha

    def get_plan(self, run_id: str, version_id: str) -> tuple[dict, str]:
        return self.plan, self.sha


class FakeApprovals:
    def __init__(self, record: ApprovalRecord | None) -> None:
        self.record = record

    def get(self, approval_id: str):
        return None if self.record is None else (self.record, TASK_TOKEN)


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def put_run_event(self, **fields) -> None:
        self.events.append(fields)


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
    """A client factory for tests in which no AWS client may even be created."""

    def __init__(self) -> None:
        self.requested: list[tuple[str, str]] = []

    def get(self, service: str, region: str):
        self.requested.append((service, region))
        raise AssertionError(f"unexpected client {service} in {region}")


class Executor:
    """The executor handler wired to fakes; ``clients`` is whatever verify/deleters will use."""

    def __init__(self, monkeypatch, candidates: list[dict], clients=None) -> None:
        self.clients = clients if clients is not None else NoClients()
        self.store = FakePlanStore(candidates)
        self.approvals = FakeApprovals(approved_record())
        self.audit = FakeAudit()
        self.events = FakeEvents()
        monkeypatch.setenv("DELETIONS_ENABLED", "true")
        monkeypatch.setattr(
            executor_handler,
            "build_dependencies",
            lambda _settings: (self.clients, self.store, self.approvals, self.audit, self.events),
        )

    def run(self, event: dict | None = None, context: FakeContext | None = None) -> dict:
        return executor_handler.handler(
            event or executor_event(), context or FakeContext(), sleep=lambda _seconds: None
        )


def outcomes(result: dict) -> list[tuple[str, str, str]]:
    return [(item["resourceId"], item["outcome"], item["reason"]) for item in result["items"]]


MUTATING_OPS = frozenset(
    {
        "delete_snapshot",
        "delete_volume",
        "release_address",
        "delete_dashboards",
        "update_monitor",
        "delete_monitor",
    }
)


class FakeAws:
    """A client factory whose clients answer from a script and record every call.

    ``answers`` maps an operation name to a response dict, a callable taking the
    call's keyword arguments, or an exception to raise. An unscripted call fails
    the test, so a delete that was never scripted can never pass unnoticed.
    """

    def __init__(self, **answers) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str, str, dict]] = []

    def get(self, service: str, region: str) -> "_FakeClient":
        return _FakeClient(self, service, region)

    @property
    def mutations(self) -> list[tuple[str, str, str, dict]]:
        return [call for call in self.calls if call[2] in MUTATING_OPS]


class _FakeClient:
    def __init__(self, owner: FakeAws, service: str, region: str) -> None:
        self._owner = owner
        self._service = service
        self._region = region

    def __getattr__(self, operation: str):
        def call(**params):
            self._owner.calls.append((self._service, self._region, operation, params))
            if operation not in self._owner.answers:
                raise AssertionError(f"unscripted AWS call {self._service}.{operation}")
            answer = self._owner.answers[operation]
            if isinstance(answer, Exception):
                raise answer
            return answer(**params) if callable(answer) else answer

        return call
