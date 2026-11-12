"""Surface 6: denial and resource exhaustion.

Regular expressions fed hostile text, plans far larger than a lab account produces,
and the two loops that keep calling AWS once per item after the time budget was
meant to have stopped the work. The time-budget tests run on a simulated clock: the
cost of one write is an assumption, stated next to each test.
"""

import json
import time

import pytest

from detector import handler as detector_handler
from executor import handler as executor_handler
from lab_janitor import config as janitor_config
from scripts import approve
from shared.dynamo import serialize
from shared.plan import build_plan
from shared.plan_store import StoredPlan
from shared.plan_summary import summarize_plan
from shared.report import render_report, truncate_report
from tests.adversarial.helpers import (
    NOW,
    RUN_ID,
    TASK_TOKEN,
    Executor,
    FakeContext,
    plan_item,
    settings,
)
from tests.adversarial.test_config_and_input import _Clients

# Generous: these inputs take milliseconds today; a catastrophic pattern takes minutes.
REGEX_BUDGET_SECONDS = 2.0


def _seconds(function, *args) -> float:
    started = time.perf_counter()
    function(*args)
    return time.perf_counter() - started


@pytest.mark.parametrize(
    ("pattern", "text"),
    [
        (janitor_config._EMAIL, "a@" + "." * 4000 + "@"),
        (janitor_config._EMAIL, "a" * 20_000 + "@" + "b" * 20_000 + "." + "c" * 20_000 + " "),
        (janitor_config._EMAIL, "@" * 50_000),
        (janitor_config._REGION, "us" + "-a" * 50_000 + "-"),
        (janitor_config._REGION, "us" + "-" * 50_000 + "1"),
        (janitor_config._ROLE_ARN, "arn:aws" + "-" * 50_000 + ":iam::123456789012:role"),
        (janitor_config._ROLE_ARN, "arn:aws:iam::123456789012:role/" + "x" * 100_000 + "\n"),
        (approve._API_HOST, "a" * 50_000 + ".execute-api." + "-" * 50_000 + ".amazonaws.com."),
        (approve._API_HOST, ("a.execute-api." * 5_000) + "amazonaws.com"),
    ],
    ids=lambda value: value.pattern[:24] if hasattr(value, "pattern") else f"{len(value)} chars",
)
def test_no_pattern_backtracks_catastrophically(pattern, text):
    assert _seconds(pattern.fullmatch, text) < REGEX_BUDGET_SECONDS


def test_hostile_context_values_are_refused_quickly():
    def synth_with(**context):
        node = type("Node", (), {"try_get_context": lambda self, key: context.get(key)})()
        with pytest.raises(ValueError, match=r"must|invalid"):
            janitor_config.JanitorConfig.from_node(node)

    assert _seconds(lambda: synth_with(reportEmail="a@" + "." * 4000 + "@")) < REGEX_BUDGET_SECONDS
    assert (
        _seconds(
            lambda: synth_with(
                reportEmail="owner@example.com", allowedRegions="us" + "-a" * 50_000 + "-"
            )
        )
        < REGEX_BUDGET_SECONDS
    )
    assert (
        _seconds(lambda: synth_with(reportEmail="owner@example.com", minAgeDays="9" * 100_000))
        < REGEX_BUDGET_SECONDS
    )


# --- huge plans ------------------------------------------------------------------------------


def _huge_plan(count: int) -> dict:
    return {
        "runId": RUN_ID,
        "generatedAt": NOW.isoformat(),
        "candidates": [
            {
                "region": "ap-southeast-2",
                "kind": "internet-monitor",
                "resourceId": "LAB-" + "\U0001f4a3" * 251,
                "reason": "name prefix LAB-",
                "estimatedMonthlyUsd": 0.01,
            }
            for _ in range(count)
        ],
        "errors": [],
    }


def test_approval_record_of_a_huge_plan_fits_a_dynamodb_item():
    """Foothold (b) floods the plan with the longest names AWS allows, in 4-byte characters.

    The record holds the summary and the task token; DynamoDB refuses items over 400 KB.
    """
    summary = summarize_plan(_huge_plan(50_000))
    item = serialize(
        {
            "pk": "APPROVAL#" + "f" * 36,
            "sk": "META",
            "planSummary": summary,
            "taskToken": TASK_TOKEN * 40,
            "approvedBy": {"mode": "link", "ip": "198.51.100.7", "userAgent": "x" * 10_240},
        }
    )

    assert summary["candidateCount"] == 50_000
    assert summary["truncated"] is True
    assert len(json.dumps(item).encode()) < 400_000 // 2


def test_summary_total_of_a_huge_plan_is_stable_and_finite():
    summary = summarize_plan(_huge_plan(50_000))

    assert summary["estimatedMonthlyUsd"] == 500.0


def test_report_of_twenty_thousand_findings_renders_and_truncates_in_linear_time():
    from shared.findings import Finding  # noqa: PLC0415 - only this test builds findings

    findings = [
        Finding(
            region="us-east-1",
            kind="ebs-snapshot",
            resource_id=f"snap-{number:017x}",
            arn=None,
            detail="1 GiB from vol-ffffffffffffffffff " + "\u00e9" * 200,
            lab=True,
            candidate=number % 2 == 0,
            reason="tag Purpose=lab, 9 days old, not referenced by any AMI",
            age_days=9,
            estimated_monthly_usd=0.05,
        )
        for number in range(20_000)
    ]
    plan = build_plan(RUN_ID, settings(), findings, [], ["us-east-1"], NOW)

    started = time.perf_counter()
    report = render_report(plan, "test-plans")
    message = truncate_report(report, 150_000, "s3://test-plans/plans/x/report.txt")
    elapsed = time.perf_counter() - started

    assert len(report.encode()) > 5_000_000
    assert len(message.encode()) <= 150_000
    assert "truncated" in message.rsplit("\n", 1)[-1]
    assert elapsed < 5.0


# --- the time budget -------------------------------------------------------------------------

WRITE_MS = 10  # assumed cost of one DynamoDB or EventBridge call from Lambda


class _Clock(FakeContext):
    """A Lambda context whose remaining time only moves when a fake AWS call spends it."""

    def spend(self, milliseconds: int) -> None:
        self.remaining_ms -= milliseconds


@pytest.mark.xfail(
    strict=True,
    reason="once the time budget trips, the executor still makes two AWS writes for every "
    "remaining item with no bound: a few thousand leftovers outlast the 30 s reserve",
)
def test_recording_the_leftovers_fits_in_the_reserve_the_executor_kept(monkeypatch):
    """Foothold (b) plus an approver who accepts a plan of thousands of items.

    Model: 3,000 items still unprocessed when the budget trips, 10 ms per write. The
    function is then killed mid-loop, Apply fails and no result email is sent; what was
    deleted before is in the audit table.
    """
    clock = _Clock(executor_handler.RESERVE_SECONDS * 1000 - 1)
    items = [plan_item("ebs-volume", f"vol-{number}") for number in range(3_000)]
    executor = Executor(monkeypatch, items)
    record_event = executor.audit.put_run_event
    put_events = executor.events.put_events
    executor.audit.put_run_event = lambda **fields: (clock.spend(WRITE_MS), record_event(**fields))
    executor.events.put_events = lambda Entries: (clock.spend(WRITE_MS), put_events(Entries))[1]

    result = executor.run(context=clock)

    assert result["skipped"] == 3_000
    assert clock.remaining_ms >= 0


class _PlanStore:
    @staticmethod
    def put(run_id: str, plan: dict, report: str) -> StoredPlan:
        return StoredPlan(f"plans/{run_id}/plan.json", "pv-1", "a" * 64, "report", "rv-1")


@pytest.mark.xfail(
    strict=True,
    reason="the detector writes one audit item per finding (more for candidates) after the "
    "sweep, with no time check: enough findings overrun the 120 s reserve and the run dies",
)
def test_audit_writes_of_a_crowded_account_fit_in_the_reserve_the_detector_kept(monkeypatch):
    """Foothold (b) alone: no approval is involved, any resource the sweep lists counts.

    Model: the sweep used its whole budget, so the 120 s reserve is all that is left;
    15,000 findings, 10 ms per write. The function times out after the plan is stored
    but before it returns, so Step Functions fails the run: no report, no approval
    request, and the next run meets the same resources.
    """
    clock = _Clock(300_000)
    plan = {
        "runId": RUN_ID,
        "candidates": [],
        "inventory": [
            {**plan_item("ebs-snapshot", f"snap-{number}"), "candidate": False}
            for number in range(15_000)
        ],
        "errors": [],
        "estimatedMonthlyUsd": 0.0,
        "dryRun": True,
        "regions": ["us-east-1"],
    }

    class Audit:
        @staticmethod
        def was_eligible_live_before(resource_id, before) -> bool:
            return False

        @staticmethod
        def put_run_event(**_fields) -> None:
            clock.spend(WRITE_MS)

        @staticmethod
        def put_run_summary(*_args, **_kwargs) -> None:
            clock.spend(WRITE_MS)

    def sweep_that_used_its_budget(run_id, ctx) -> dict:
        clock.remaining_ms = detector_handler.UPLOAD_RESERVE_SECONDS * 1000
        return plan

    monkeypatch.setattr(
        detector_handler,
        "build_dependencies",
        lambda _settings: (_Clients(), _PlanStore(), Audit()),
    )
    monkeypatch.setattr(detector_handler, "detect_plan", sweep_that_used_its_budget)

    result = detector_handler.handler({}, clock)

    assert result["findingCount"] == 15_000
    assert clock.remaining_ms >= 0
