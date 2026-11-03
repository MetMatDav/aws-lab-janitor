import logging
import re
import time
from datetime import datetime

import pytest

from detector import handler as handler_module
from detector.sweep import SweepContext
from shared.findings import ErrorFinding, Finding
from shared.plan import plan_bytes, plan_sha256
from shared.plan_store import StoredPlan

ACCOUNT = "123456789012"
REGIONS = ["eu-west-1", "us-east-1"]
RESULT_KEYS = {
    "runId",
    "planKey",
    "planVersionId",
    "planSha256",
    "reportKey",
    "reportVersionId",
    "candidateCount",
    "errorCount",
    "findingCount",
    "estimatedMonthlyUsd",
    "dryRun",
}
REGIONAL = ("detect_ec2", "detect_rds", "detect_elasticache", "detect_elbv2")
ALL_REGIONAL = (*REGIONAL, "detect_internet_monitor")


def finding(resource_id: str, region: str = "us-east-1", **overrides) -> Finding:
    values = {
        "region": region,
        "kind": "nat-gateway",
        "resource_id": resource_id,
        "arn": None,
        "detail": "available",
        "lab": True,
        "candidate": False,
        "reason": "not-old-enough",
        "age_days": 2,
        "estimated_monthly_usd": 10.0,
    }
    values.update(overrides)
    return Finding(**values)


class FakeSts:
    def get_caller_identity(self) -> dict:
        return {"Account": ACCOUNT, "Arn": "arn", "UserId": "u"}


class FakeFactory:
    def __init__(self, regions: list[str]) -> None:
        self.regions = regions
        self.requested: list[tuple[str, str]] = []

    def get(self, service: str, region: str):
        self.requested.append((service, region))
        assert service == "sts"
        return FakeSts()

    def enabled_regions(self) -> list[str]:
        return list(self.regions)


class FakeStore:
    def __init__(self) -> None:
        self.puts: list[tuple[str, dict, str]] = []

    def put(self, run_id: str, plan: dict, report: str) -> StoredPlan:
        self.puts.append((run_id, plan, report))
        return StoredPlan(
            plan_key=f"plans/{run_id}/plan.json",
            plan_version_id="pv-1",
            plan_sha256=plan_sha256(plan_bytes(plan)),
            report_key=f"plans/{run_id}/report.txt",
            report_version_id="rv-1",
        )


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.summaries: list[tuple[str, dict, datetime]] = []

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

    def was_proposed_live(self, resource_id: str) -> bool:
        return False

    def put_run_summary(self, run_id: str, summary: dict, at: datetime) -> None:
        self.summaries.append((run_id, summary, at))


class FakeContext:
    def __init__(self, remaining_ms: int) -> None:
        self.remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self.remaining_ms


class Harness:
    """Wires fakes into the handler module and records detector calls."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, regions: list[str]) -> None:
        self.factory = FakeFactory(regions)
        self.store = FakeStore()
        self.audit = FakeAudit()
        self.calls: list[tuple[str, str]] = []
        self.contexts: list[SweepContext] = []
        self.regional_findings: dict[str, list[Finding]] = {}
        self.global_findings: list[Finding] = []
        self.settings = None

        def fake_dependencies(settings):
            self.settings = settings
            return self.factory, self.store, self.audit

        monkeypatch.setattr(handler_module, "build_dependencies", fake_dependencies)
        for name in ALL_REGIONAL:
            monkeypatch.setattr(handler_module, name, self._regional(name))
        monkeypatch.setattr(handler_module, "detect_global", self._global)

    def _regional(self, name: str):
        def detect(region, ctx, findings, errors):
            self.calls.append((name, region))
            self.contexts.append(ctx)
            if name == "detect_ec2":
                findings.extend(self.regional_findings.get(region, []))

        return detect

    def _global(self, ctx, findings, errors):
        self.calls.append(("detect_global", "global"))
        self.contexts.append(ctx)
        findings.extend(self.global_findings)

    @property
    def plan(self) -> dict:
        assert len(self.store.puts) == 1
        return self.store.puts[0][1]


@pytest.fixture
def harness(monkeypatch) -> Harness:
    return Harness(monkeypatch, REGIONS)


def three_findings(harness: Harness) -> None:
    harness.regional_findings = {
        "us-east-1": [
            finding("nat-1", candidate=True, reason="lab-tag", estimated_monthly_usd=32.85),
            finding("vol-1", kind="ebs-volume", estimated_monthly_usd=0.8),
        ],
    }
    harness.global_findings = [
        finding("dash-1", region="global", kind="cw-dashboard", estimated_monthly_usd=None)
    ]


def test_result_is_compact_and_matches_plan(harness):
    three_findings(harness)

    result = handler_module.handler({"config": {"dryRun": True, "minAgeDays": 7}}, None)

    assert set(result) == RESULT_KEYS
    assert "candidates" not in result
    assert "report" not in result
    plan = harness.plan
    run_id, stored_plan, report = harness.store.puts[0]
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{4}", result["runId"])
    assert run_id == result["runId"] == plan["runId"]
    assert result["planKey"] == f"plans/{run_id}/plan.json"
    assert result["planVersionId"] == "pv-1"
    assert result["planSha256"] == plan_sha256(plan_bytes(stored_plan))
    assert result["reportKey"] == f"plans/{run_id}/report.txt"
    assert result["reportVersionId"] == "rv-1"
    assert result["candidateCount"] == len(plan["candidates"]) == 1
    assert result["findingCount"] == 3
    assert result["errorCount"] == len(plan["errors"]) == 0
    assert result["estimatedMonthlyUsd"] == plan["estimatedMonthlyUsd"] == 33.65
    assert result["dryRun"] is True
    assert plan["regions"] == REGIONS
    assert "LAB JANITOR WEEKLY REPORT" in report
    assert f"s3://test-plans/plans/{run_id}/plan.json" in report


def test_audit_events_detected_and_proposed(harness):
    three_findings(harness)

    result = handler_module.handler({"config": {"dryRun": False}}, None)

    events = harness.audit.events
    detected = [e for e in events if e["event"] == "DETECTED"]
    proposed = [e for e in events if e["event"] == "PROPOSED"]
    assert len(events) == 4
    assert {e["resource_id"] for e in detected} == {"nat-1", "vol-1", "dash-1"}
    assert [e["resource_id"] for e in proposed] == ["nat-1"]
    nat = proposed[0]
    assert nat["region"] == "us-east-1"
    assert nat["detail"] == {
        "kind": "nat-gateway",
        "reason": "lab-tag",
        "estimatedMonthlyUsd": 32.85,
        "planSha256": result["planSha256"],
    }
    dash = next(e for e in detected if e["resource_id"] == "dash-1")
    assert dash["region"] == "global"
    assert dash["detail"]["estimatedMonthlyUsd"] is None
    at = events[0]["at"]
    for event in events:
        assert event["run_id"] == result["runId"]
        assert event["actor"] == "lambda:detector"
        assert event["dry_run"] is False
        assert event["at"] == at
        assert event["detail"]["planSha256"] == result["planSha256"]
        assert event["kind"] == event["detail"]["kind"]
    kinds = {(e["resource_id"], e["event"]): e["kind"] for e in events}
    assert kinds == {
        ("nat-1", "DETECTED"): "nat-gateway",
        ("nat-1", "PROPOSED"): "nat-gateway",
        ("vol-1", "DETECTED"): "ebs-volume",
        ("dash-1", "DETECTED"): "cw-dashboard",
    }

    assert harness.audit.summaries == [(result["runId"], result, at)]


def test_same_name_bucket_and_dashboard_get_distinct_events(harness):
    harness.global_findings = [
        finding("LAB-shared", region="global", kind="s3-bucket", estimated_monthly_usd=None),
        finding("LAB-shared", region="global", kind="cw-dashboard", estimated_monthly_usd=None),
    ]

    result = handler_module.handler({"config": {"dryRun": True}}, None)

    detected = [e for e in harness.audit.events if e["event"] == "DETECTED"]
    assert result["findingCount"] == len(detected) == 2
    assert {(e["region"], e["kind"], e["resource_id"]) for e in detected} == {
        ("global", "s3-bucket", "LAB-shared"),
        ("global", "cw-dashboard", "LAB-shared"),
    }


def test_region_failure_becomes_error_not_crash(harness, monkeypatch):
    def broken(region, ctx, findings, errors):
        if region == "eu-west-1":
            raise RuntimeError("boom")

    monkeypatch.setattr(handler_module, "detect_ec2", broken)

    result = handler_module.handler({"config": {"dryRun": True}}, None)

    assert result["errorCount"] == 1
    assert len(harness.store.puts) == 1
    [error] = harness.plan["errors"]
    assert error["region"] == "eu-west-1"
    assert error["service"] == "ec2"
    assert error["classification"] == "other"
    assert "boom" in error["error"]
    # The other detectors in the failing region still ran.
    assert ("detect_rds", "eu-west-1") in harness.calls


def test_time_budget_exhausted_still_writes_plan(harness):
    result = handler_module.handler({"config": {"dryRun": True}}, FakeContext(1000))

    assert harness.calls == []
    assert len(harness.store.puts) == 1
    errors = harness.plan["errors"]
    assert result["errorCount"] == len(REGIONS) + 1
    assert sorted(e["region"] for e in errors) == sorted([*REGIONS, "global"])
    for error in errors:
        assert error["service"] == "sweep"
        assert error["error"] == "skipped: time budget exhausted"
        assert error["classification"] == "time-budget"
    assert len(harness.audit.summaries) == 1


def test_missing_config_is_dry_run(harness):
    result = handler_module.handler({}, None)

    assert result["dryRun"] is True
    assert harness.settings.dry_run is True
    assert harness.plan["dryRun"] is True


def test_context_carries_account_audit_lookup_and_deadline(harness):
    before = time.time()
    handler_module.handler({"config": {"dryRun": True}}, FakeContext(600_000))
    after = time.time()

    assert ("sts", harness.settings.region) in harness.factory.requested
    assert harness.contexts
    ctx = harness.contexts[0]
    assert all(c is ctx for c in harness.contexts)
    assert ctx.account == ACCOUNT
    assert ctx.was_proposed_live == harness.audit.was_proposed_live
    assert before + 600 - 120 <= ctx.deadline <= after + 600 - 120
    assert ctx.settings is harness.settings
    assert isinstance(ctx.log, logging.LoggerAdapter)
    assert ctx.log.extra == {"runId": harness.plan["runId"]}


def test_local_deadline_without_context(harness):
    before = time.time()
    handler_module.handler({"config": {"dryRun": True}}, None)
    after = time.time()

    deadline = harness.contexts[0].deadline
    assert before + 240 <= deadline <= after + 240


def test_every_detector_runs_for_every_region(harness):
    handler_module.handler({"config": {"dryRun": True}}, None)

    expected = {(name, region) for name in ALL_REGIONAL for region in REGIONS}
    expected.add(("detect_global", "global"))
    assert set(harness.calls) == expected
    assert len(harness.calls) == len(expected)


def test_sweep_gathers_results_in_region_order(monkeypatch, ctx_factory):
    regions = ["r-slow", "r-mid", "r-fast"]
    delays = {"r-slow": 0.2, "r-mid": 0.1, "r-fast": 0.0}

    def ec2(region, ctx, findings, errors):
        time.sleep(delays[region])
        findings.append(finding(f"f-{region}", region=region))
        errors.append(ErrorFinding(region, "ec2:X", "e", "other"))

    def noop(*_args):
        return None

    monkeypatch.setattr(handler_module, "detect_ec2", ec2)
    for name in (*REGIONAL[1:], "detect_internet_monitor"):
        monkeypatch.setattr(handler_module, name, noop)

    def global_detector(ctx, findings, errors):
        findings.append(finding("f-global", region="global"))

    monkeypatch.setattr(handler_module, "detect_global", global_detector)

    findings, errors = handler_module.sweep(regions, ctx_factory())

    assert [f.region for f in findings] == [*regions, "global"]
    assert [e.region for e in errors] == regions


def test_sweep_region_guard_skips_detectors(monkeypatch, ctx_past_deadline):
    called = []
    for name in ALL_REGIONAL:
        monkeypatch.setattr(handler_module, name, lambda *args: called.append(args))

    findings, errors = handler_module.sweep_region("us-east-1", ctx_past_deadline)

    assert called == []
    assert findings == []
    assert errors == [
        ErrorFinding("us-east-1", "sweep", "skipped: time budget exhausted", "time-budget")
    ]


def test_global_guard_skips_detect_global(monkeypatch, ctx_past_deadline):
    called = []
    monkeypatch.setattr(handler_module, "detect_global", lambda *args: called.append(args))

    findings, errors = handler_module.sweep_global(ctx_past_deadline)

    assert called == []
    assert findings == []
    assert errors == [
        ErrorFinding("global", "sweep", "skipped: time budget exhausted", "time-budget")
    ]
