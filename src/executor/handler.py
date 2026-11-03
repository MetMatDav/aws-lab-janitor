"""Executor Lambda: apply exactly the approved version of a plan.

It reads the plan version the human approved, checks that the plan, the
detector's hash and the approval record all agree, and then walks the plan's
candidates in order. It never lists resources: an id absent from the plan is
never touched. Each item is re-verified against its live state before it is
deleted, and every item ends with one DELETED, FAILED or SKIPPED audit record
and the matching EventBridge event. Nothing is deleted unless the function's
``DELETIONS_ENABLED`` flag is ``true``, the run's ``dryRun`` is the boolean
False and the approval decision is ``approved``.
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime

from botocore.client import BaseClient
from botocore.exceptions import ClientError

from executor import deleters, verify
from shared.approvals import APPROVED, ApprovalStore
from shared.audit import AuditClient
from shared.aws_clients import ClientFactory
from shared.plan_store import PlanIntegrityError, PlanStore
from shared.settings import Settings, load_settings

ACTOR = "lambda:executor"
# Seconds kept back from the Lambda timeout; below this, the remaining items are skipped.
RESERVE_SECONDS = 30
EVENT_SOURCE = "lab-janitor"
DETAIL_TYPE = "janitor.resource.{outcome}"
EVENT_BUS_NAME = "default"
MAX_RESULT_ITEMS = 200
DELETABLE_KINDS = ("ebs-snapshot", "ebs-volume", "elastic-ip", "cw-dashboard", "internet-monitor")

DELETED = "DELETED"
FAILED = "FAILED"
SKIPPED = "SKIPPED"


def build_dependencies(
    settings: Settings,
) -> tuple[ClientFactory, PlanStore, ApprovalStore, AuditClient, BaseClient]:
    """Create the client factory, plan store, approval store, audit client and events client."""
    clients = ClientFactory()
    ddb = clients.get("dynamodb", settings.region)
    store = PlanStore(clients.get("s3", settings.region), settings.plans_bucket)
    approvals = ApprovalStore(ddb, settings.audit_table)
    audit = AuditClient(ddb, settings.audit_table)
    return clients, store, approvals, audit, clients.get("events", settings.region)


def required_seconds(kind: str) -> int:
    """Seconds that must remain before an item of ``kind`` starts.

    A monitor may be polled for up to ``MONITOR_WAIT_SECONDS`` before its delete, so it
    needs that window on top of the reserve.
    """
    if kind == "internet-monitor":
        return RESERVE_SECONDS + deleters.MONITOR_WAIT_SECONDS
    return RESERVE_SECONDS


def check_integrity(detect: dict, approval_id: str, plan_sha: str, found) -> None:
    """Raise ``PlanIntegrityError`` unless the plan read, Detect and the approval all agree."""
    run_id = detect["runId"]
    if found is None:
        raise PlanIntegrityError(f"run {run_id}: approval record not found")
    # The task token (second element) is never read, so it can never be logged.
    record, _ = found
    checks = {
        "plan sha256 differs from Detect": plan_sha == detect["planSha256"],
        "plan sha256 differs from the approval": plan_sha == record.plan_sha256,
        "approval belongs to another run": record.run_id == run_id,
        "approval is not APPROVED": record.status == APPROVED,
        "approval id differs": record.approval_id == approval_id,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise PlanIntegrityError(f"run {run_id}: {', '.join(failed)}")


def _error_code(error: ClientError) -> str:
    return error.response.get("Error", {}).get("Code", "ClientError")


@dataclass(frozen=True)
class _Run:
    """One executor invocation: what it may do and where it records each outcome."""

    run_id: str
    approval_id: str
    plan_sha: str
    armed: bool
    settings: Settings
    clients: ClientFactory
    audit: AuditClient
    events: BaseClient
    context: object
    log: logging.LoggerAdapter

    def remaining_seconds(self) -> float:
        return self.context.get_remaining_time_in_millis() / 1000

    def gate(self, item: dict) -> str | None:
        """Why ``item`` is skipped before any AWS call, or None: time, arming, then kind."""
        if self.remaining_seconds() < required_seconds(item["kind"]):
            return "time-budget"
        if not self.armed:
            return "deletions-disabled"
        if item["kind"] not in DELETABLE_KINDS:
            return "no-deleter"
        return None

    def apply(self, item: dict, now: datetime) -> tuple[str, str]:
        """(outcome, reason) for one item; the first blocker wins.

        A ``ClientError`` becomes FAILED with its code; any other exception is
        audited as FAILED ``exception`` and then propagates.
        """
        blocker = self.gate(item)
        if blocker is not None:
            return SKIPPED, blocker
        try:
            reason = verify.recheck(item, self.clients, self.settings, now)
            if reason is not None:
                return SKIPPED, reason
            self.log.info("deleting %s %s in %s", item["kind"], item["resourceId"], item["region"])
            deleters.delete(item, self.clients)
        except deleters.MonitorStillActive:
            return FAILED, "monitor-still-active"
        except ClientError as error:
            return FAILED, _error_code(error)
        except Exception:
            self.record(item, FAILED, "exception", now)
            raise
        return DELETED, item["reason"]

    def record(self, item: dict, outcome: str, reason: str, now: datetime) -> None:
        """Write the audit record of ``item`` and emit the same record as an event."""
        detail = {
            "reason": reason,
            "approvalId": self.approval_id,
            "planSha256": self.plan_sha,
            "estimatedMonthlyUsd": item.get("estimatedMonthlyUsd"),
        }
        self.audit.put_run_event(
            run_id=self.run_id,
            region=item["region"],
            resource_id=item["resourceId"],
            kind=item["kind"],
            event=outcome,
            actor=ACTOR,
            dry_run=False,
            detail=detail,
            at=now,
        )
        event_detail = {
            "runId": self.run_id,
            "region": item["region"],
            "resourceId": item["resourceId"],
            "kind": item["kind"],
            "event": outcome,
            "actor": ACTOR,
            "dryRun": False,
            "detail": detail,
            "at": now.astimezone(UTC).isoformat(),
        }
        response = self.events.put_events(
            Entries=[
                {
                    "Source": EVENT_SOURCE,
                    "DetailType": DETAIL_TYPE.format(outcome=outcome.lower()),
                    "Detail": json.dumps(event_detail, default=str),
                    "EventBusName": EVENT_BUS_NAME,
                }
            ]
        )
        if response.get("FailedEntryCount"):
            self.log.warning("event for %s %s was not accepted", item["kind"], item["resourceId"])
        self.log.info("%s %s %s: %s", outcome, item["kind"], item["resourceId"], reason)


def handler(event: dict, context) -> dict:
    """Lambda entry point: verify integrity, apply each plan item, return per-item outcomes."""
    settings = load_settings(event, os.environ)
    detect = event["detect"]
    run_id = detect["runId"]
    approval_id = event["approvalId"]
    decision = (event.get("approval") or {}).get("decision")
    armed = settings.deletions_enabled and settings.dry_run is False and decision == "approved"
    log = logging.LoggerAdapter(logging.getLogger("executor"), {"runId": run_id})

    clients, store, approvals, audit, events = build_dependencies(settings)
    plan, plan_sha = store.get_plan(run_id, detect["planVersionId"])
    check_integrity(detect, approval_id, plan_sha, approvals.get(approval_id))
    log.info("executor run started (armed=%s)", armed)

    run = _Run(
        run_id=run_id,
        approval_id=approval_id,
        plan_sha=plan_sha,
        armed=armed,
        settings=settings,
        clients=clients,
        audit=audit,
        events=events,
        context=context,
        log=log,
    )
    counts = {DELETED: 0, FAILED: 0, SKIPPED: 0}
    items: list[dict] = []
    for item in plan["candidates"]:
        now = datetime.now(UTC)
        outcome, reason = run.apply(item, now)
        run.record(item, outcome, reason, now)
        counts[outcome] += 1
        if len(items) < MAX_RESULT_ITEMS:
            items.append(
                {
                    "resourceId": item["resourceId"],
                    "kind": item["kind"],
                    "region": item["region"],
                    "outcome": outcome,
                    "reason": reason,
                }
            )
    log.info(
        "executor run finished: %d deleted, %d failed, %d skipped",
        counts[DELETED],
        counts[FAILED],
        counts[SKIPPED],
    )
    return {
        "runId": run_id,
        "approvalId": approval_id,
        "deleted": counts[DELETED],
        "failed": counts[FAILED],
        "skipped": counts[SKIPPED],
        "items": items,
    }
