"""Detector Lambda: sweep the account, store the immutable plan and report, audit the run.

The detector only observes. It never deletes anything; it writes the plan and
its report to the versioned plans bucket, one ``DETECTED`` audit event per
finding, one ``PROPOSED`` event per candidate and a run summary, and returns a
compact result for Step Functions.

Run locally with ``PYTHONPATH=src python -m detector.handler`` to print a
dry-run plan built with your own credentials; nothing is uploaded or audited.
"""

import logging
import os
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from detector.detectors.databases import detect_elasticache, detect_rds
from detector.detectors.ec2 import detect_ec2
from detector.detectors.elb import detect_elbv2
from detector.detectors.global_ import detect_global
from detector.detectors.monitor import detect_internet_monitor
from detector.sweep import TIME_BUDGET_MESSAGE, SweepContext, safe
from shared.audit import AuditClient
from shared.aws_clients import ClientFactory
from shared.findings import ErrorFinding, Finding
from shared.plan import build_plan, new_run_id, plan_bytes
from shared.plan_store import PlanStore
from shared.report import render_report
from shared.settings import Settings, load_settings

ACTOR = "lambda:detector"
GLOBAL_REGION = "global"
MAX_WORKERS = 12
# Seconds kept back from the Lambda timeout for the plan upload and the audit writes.
UPLOAD_RESERVE_SECONDS = 45
# Sweep budget when there is no Lambda context (local runs).
LOCAL_BUDGET_SECONDS = 240
SWEEP_SERVICE = "sweep"
TIME_BUDGET_CLASSIFICATION = "time-budget"

Sweep = tuple[list[Finding], list[ErrorFinding]]


def build_dependencies(settings: Settings) -> tuple[ClientFactory, PlanStore, AuditClient]:
    """Create the client factory, the plan store and the audit client for one invocation."""
    clients = ClientFactory()
    store = PlanStore(clients.get("s3", settings.region), settings.plans_bucket)
    audit = AuditClient(clients.get("dynamodb", settings.region), settings.audit_table)
    return clients, store, audit


def deadline_for(context) -> float:
    """Epoch second after which no new region or detector starts."""
    if context is None:
        return time.time() + LOCAL_BUDGET_SECONDS
    remaining = context.get_remaining_time_in_millis() / 1000
    return time.time() + remaining - UPLOAD_RESERVE_SECONDS


def _budget_exhausted(region: str, ctx: SweepContext, errors: list[ErrorFinding]) -> bool:
    """Record a time-budget error for ``region`` and return True once the deadline has passed."""
    if time.time() <= ctx.deadline:
        return False
    errors.append(
        ErrorFinding(region, SWEEP_SERVICE, TIME_BUDGET_MESSAGE, TIME_BUDGET_CLASSIFICATION)
    )
    ctx.log.warning("skipping %s: time budget exhausted", region)
    return True


def sweep_region(region: str, ctx: SweepContext) -> Sweep:
    """Run every regional detector for ``region``; skip them all once the budget is spent.

    Each detector runs inside ``safe`` so an unexpected exception becomes an
    error for that service instead of aborting the region.
    """
    findings: list[Finding] = []
    errors: list[ErrorFinding] = []
    if _budget_exhausted(region, ctx, errors):
        return findings, errors
    # Looked up at call time so the module-level names stay the single source of truth.
    detectors: tuple[tuple[str, Callable[..., None]], ...] = (
        ("ec2", detect_ec2),
        ("rds", detect_rds),
        ("elasticache", detect_elasticache),
        ("elasticloadbalancing", detect_elbv2),
        ("internetmonitor", detect_internet_monitor),
    )
    for service, detect in detectors:
        safe(ctx, errors, region, service, lambda d=detect: d(region, ctx, findings, errors))
    return findings, errors


def sweep_global(ctx: SweepContext) -> Sweep:
    """Run the global detector under the same time-budget guard as a region."""
    findings: list[Finding] = []
    errors: list[ErrorFinding] = []
    if _budget_exhausted(GLOBAL_REGION, ctx, errors):
        return findings, errors
    safe(ctx, errors, GLOBAL_REGION, GLOBAL_REGION, lambda: detect_global(ctx, findings, errors))
    return findings, errors


def sweep(regions: list[str], ctx: SweepContext) -> Sweep:
    """Sweep ``regions`` in parallel, then the global services; results keep region order."""
    findings: list[Finding] = []
    errors: list[ErrorFinding] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(lambda region: sweep_region(region, ctx), regions))
    results.append(sweep_global(ctx))
    for region_findings, region_errors in results:
        findings.extend(region_findings)
        errors.extend(region_errors)
    return findings, errors


def start_run(
    settings: Settings,
    clients: ClientFactory,
    was_proposed_live: Callable[[str], bool],
    context,
) -> tuple[str, SweepContext]:
    """Open a run: its id, its run-scoped logger and the context every detector receives."""
    now = datetime.now(UTC)
    run_id = new_run_id(now)
    log = logging.LoggerAdapter(logging.getLogger("detector"), {"runId": run_id})
    account = clients.get("sts", settings.region).get_caller_identity()["Account"]
    ctx = SweepContext(
        settings=settings,
        clients=clients,
        now=now,
        account=account,
        was_proposed_live=was_proposed_live,
        deadline=deadline_for(context),
        log=log,
    )
    ctx.log.info("detector run started (dryRun=%s)", settings.dry_run)
    return run_id, ctx


def detect_plan(run_id: str, ctx: SweepContext) -> dict:
    """Sweep every enabled region plus the global services and build the plan document."""
    regions = ctx.clients.enabled_regions()
    findings, errors = sweep(regions, ctx)
    ctx.log.info(
        "sweep finished: %d regions, %d findings, %d errors",
        len(regions),
        len(findings),
        len(errors),
    )
    return build_plan(run_id, ctx.settings, findings, errors, regions, ctx.now)


def handler(event: dict, context) -> dict:
    """Lambda entry point: detect, store the plan and report, audit, return a compact result."""
    settings = load_settings(event, os.environ)
    clients, store, audit = build_dependencies(settings)
    run_id, ctx = start_run(settings, clients, audit.was_proposed_live, context)
    plan = detect_plan(run_id, ctx)

    stored = store.put(run_id, plan, render_report(plan, settings.plans_bucket))
    ctx.log.info(
        "plan stored at %s (version %s, sha256 %s)",
        stored.plan_key,
        stored.plan_version_id,
        stored.plan_sha256,
    )

    candidates = plan["candidates"]
    detected = [*candidates, *plan["inventory"]]
    for event_name, items in (("DETECTED", detected), ("PROPOSED", candidates)):
        for item in items:
            audit.put_run_event(
                run_id=run_id,
                region=item["region"],
                resource_id=item["resourceId"],
                event=event_name,
                actor=ACTOR,
                dry_run=settings.dry_run,
                detail={
                    "kind": item["kind"],
                    "reason": item["reason"],
                    "estimatedMonthlyUsd": item["estimatedMonthlyUsd"],
                    "planSha256": stored.plan_sha256,
                },
                at=ctx.now,
            )

    result = {
        "runId": run_id,
        "planKey": stored.plan_key,
        "planVersionId": stored.plan_version_id,
        "planSha256": stored.plan_sha256,
        "reportKey": stored.report_key,
        "reportVersionId": stored.report_version_id,
        "candidateCount": len(candidates),
        "errorCount": len(plan["errors"]),
        "findingCount": len(detected),
        "estimatedMonthlyUsd": plan["estimatedMonthlyUsd"],
        "dryRun": settings.dry_run,
    }
    audit.put_run_summary(run_id, result, ctx.now)
    return result


def main() -> None:
    """Print a dry-run plan for the current credentials; no upload and no audit writes.

    No audit table is read either, so no resource counts as previously proposed live.
    """
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    settings = load_settings({"config": {"dryRun": True}}, os.environ)
    clients = ClientFactory()
    run_id, ctx = start_run(settings, clients, lambda _resource_id: False, None)
    print(plan_bytes(detect_plan(run_id, ctx)).decode())


if __name__ == "__main__":
    main()
