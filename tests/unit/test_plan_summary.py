from shared.dynamo import deserialize, serialize
from shared.plan_summary import render_summary, summarize_plan


def candidate(resource_id: str, region: str = "us-east-1", cost: float | None = None) -> dict:
    return {
        "region": region,
        "kind": "nat-gateway",
        "resourceId": resource_id,
        "arn": None,
        "detail": "",
        "lab": True,
        "candidate": True,
        "eligible": True,
        "reason": "idle\nfor 30 days",
        "ageDays": 30,
        "estimatedMonthlyUsd": cost,
    }


def plan_with(*candidates: dict, errors: int = 0) -> dict:
    return {
        "runId": "20261102T120000Z-abc1",
        "generatedAt": "2026-11-02T12:00:00+00:00",
        "candidates": list(candidates),
        "errors": [{}] * errors,
    }


def test_summary_counts_costs_and_regions():
    plan = plan_with(
        candidate("nat-1", "us-west-2", 32.85),
        candidate("nat-2", "eu-west-1"),
        candidate("nat-3", "us-west-2", 0.1),
        errors=2,
    )

    summary = summarize_plan(plan)

    assert summary["runId"] == "20261102T120000Z-abc1"
    assert summary["generatedAt"] == "2026-11-02T12:00:00+00:00"
    assert summary["candidateCount"] == 3
    assert summary["estimatedMonthlyUsd"] == 32.95
    assert summary["errorCount"] == 2
    assert summary["regions"] == ["eu-west-1", "us-west-2"]
    assert summary["truncated"] is False
    assert summary["candidates"][1] == {
        "region": "eu-west-1",
        "kind": "nat-gateway",
        "resourceId": "nat-2",
        "reason": "idle\nfor 30 days",
        "estimatedMonthlyUsd": None,
    }


def test_summary_truncates_at_limit_in_plan_order():
    plan = plan_with(candidate("nat-1", cost=1.0), candidate("nat-2", cost=2.0), candidate("nat-3"))

    summary = summarize_plan(plan, limit=2)

    assert summary["truncated"] is True
    assert [c["resourceId"] for c in summary["candidates"]] == ["nat-1", "nat-2"]
    assert summary["candidateCount"] == 3


def test_summary_of_empty_plan():
    summary = summarize_plan(plan_with())

    assert summary["candidateCount"] == 0
    assert summary["estimatedMonthlyUsd"] == 0
    assert summary["regions"] == []
    assert summary["candidates"] == []
    assert summary["truncated"] is False


def test_render_summary_lines():
    plan = plan_with(candidate("nat-1", "us-west-2", 32.85), candidate("nat-2"), candidate("nat-3"))

    text = render_summary(summarize_plan(plan, limit=2))

    assert text.split("\n") == [
        "[nat-gateway] nat-1 (us-west-2) idle for 30 days ~$32.85/mo",
        "[nat-gateway] nat-2 (us-east-1) idle for 30 days",
        "... and 1 more",
    ]


def test_render_summary_without_truncation_has_no_more_line():
    text = render_summary(summarize_plan(plan_with(candidate("nat-1", cost=1.0))))

    assert text == "[nat-gateway] nat-1 (us-east-1) idle for 30 days ~$1.00/mo"


def test_render_summary_of_a_summary_read_back_from_dynamodb():
    summary = summarize_plan(plan_with(candidate("nat-1", cost=1.5), candidate("nat-2")))
    stored = deserialize(serialize({"planSummary": summary}))["planSummary"]

    text = render_summary(stored)

    assert text.splitlines() == [
        "[nat-gateway] nat-1 (us-east-1) idle for 30 days ~$1.50/mo",
        "[nat-gateway] nat-2 (us-east-1) idle for 30 days",
    ]
