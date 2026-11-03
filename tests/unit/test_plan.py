import json
import re
from dataclasses import replace
from datetime import UTC, datetime

from shared.findings import ErrorFinding, Finding
from shared.plan import build_plan, new_run_id, plan_bytes, plan_sha256
from shared.pricing import PRICES_AS_OF, PRICES_REGION
from shared.settings import Settings

NOW = datetime(2026, 11, 10, 14, 0, 12, tzinfo=UTC)
SETTINGS = Settings(
    dry_run=True,
    min_age_days=7,
    name_prefix="lab-",
    tag_key="env",
    tag_value="lab",
    keep_tag_key="janitor",
    keep_tag_value="keep",
    plans_bucket="plans-bucket",
    audit_table="audit",
    reports_topic_arn="arn:aws:sns:us-east-1:123456789012:reports",
    region="us-east-1",
)
PLAN_KEYS = {
    "runId",
    "generatedAt",
    "dryRun",
    "criteria",
    "regions",
    "candidates",
    "inventory",
    "errors",
    "estimatedMonthlyUsd",
    "pricesAsOf",
    "pricesRegion",
}


def finding(resource_id: str, **overrides) -> Finding:
    """A lab NAT gateway candidate in us-east-1; keyword arguments override any field."""
    values = {
        "region": "us-east-1",
        "kind": "nat-gateway",
        "resource_id": resource_id,
        "arn": None,
        "detail": "available",
        "lab": True,
        "candidate": True,
        "reason": "lab-tag",
        "age_days": 12,
        "estimated_monthly_usd": 32.85,
    }
    values.update(overrides)
    return Finding(**values)


def sample_inputs() -> tuple[list[Finding], list[ErrorFinding]]:
    findings = [
        finding("nat-2", region="us-west-2"),
        finding("nat-1"),
        finding(
            "vol-1",
            kind="ebs-volume",
            candidate=False,
            lab=False,
            reason="not-lab",
            estimated_monthly_usd=0.8,
        ),
        finding(
            "dash-1",
            region="global",
            kind="cw-dashboard",
            candidate=False,
            estimated_monthly_usd=None,
            age_days=None,
        ),
    ]
    errors = [
        ErrorFinding("eu-west-1", "rds:DescribeDBInstances", "AccessDenied", "access-denied"),
        ErrorFinding("ap-south-1", "ec2:DescribeAddresses", "boom", "other"),
    ]
    return findings, errors


def make_plan(findings, errors, run_id="20261110T140012Z-7f3a"):
    regions = ["us-east-1", "us-west-2", "eu-west-1", "ap-south-1"]
    return build_plan(run_id, SETTINGS, findings, errors, regions, NOW)


def test_new_run_id_format():
    run_id = new_run_id(NOW)
    assert re.fullmatch(r"20261110T140012Z-[0-9a-f]{4}", run_id)


def test_plan_shape_and_hash_stability():
    findings, errors = sample_inputs()
    plan = make_plan(findings, errors)

    assert set(plan) == PLAN_KEYS
    assert plan["runId"] == "20261110T140012Z-7f3a"
    assert datetime.fromisoformat(plan["generatedAt"]) == NOW
    assert plan["dryRun"] is True
    assert plan["criteria"] == {
        "tagKey": "env",
        "tagValue": "lab",
        "namePrefix": "lab-",
        "keepTag": "janitor=keep",
        "minAgeDays": 7,
    }
    assert plan["regions"] == ["us-east-1", "us-west-2", "eu-west-1", "ap-south-1"]
    assert [c["resourceId"] for c in plan["candidates"]] == ["nat-1", "nat-2"]
    assert all(c["candidate"] for c in plan["candidates"])
    assert [i["resourceId"] for i in plan["inventory"]] == ["dash-1", "vol-1"]
    assert plan["candidates"][0] == finding("nat-1").to_dict()
    assert len(plan["errors"]) == 2
    assert plan["estimatedMonthlyUsd"] == 66.5
    assert plan["pricesAsOf"] == PRICES_AS_OF
    assert plan["pricesRegion"] == PRICES_REGION

    reordered = make_plan(list(reversed(findings)), list(reversed(errors)))
    digest = plan_sha256(plan_bytes(plan))
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert plan_sha256(plan_bytes(reordered)) == digest
    assert plan_bytes(plan) == plan_bytes(reordered)


def test_plan_bytes_are_canonical_json():
    plan = make_plan([], [])
    expected = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    assert plan_bytes(plan) == expected


def test_plan_hash_changes_with_content():
    findings, errors = sample_inputs()
    plan = make_plan(findings, errors)
    other = make_plan([*findings[:-1], replace(findings[-1], detail="changed")], errors)
    assert plan_sha256(plan_bytes(plan)) != plan_sha256(plan_bytes(other))


def test_plan_live_mode_and_empty_cost():
    live = replace(SETTINGS, dry_run=False)
    plan = build_plan("r", live, [finding("x", estimated_monthly_usd=None)], [], [], NOW)
    assert plan["dryRun"] is False
    assert plan["estimatedMonthlyUsd"] == 0


def test_empty_plan_cost_serializes_as_float_zero():
    plan = make_plan([], [])
    assert isinstance(plan["estimatedMonthlyUsd"], float)
    assert b'"estimatedMonthlyUsd":0.0' in plan_bytes(plan)


def test_arn_breaks_ties_between_same_region_kind_and_id():
    first = finding("dup", arn="arn:aws:ec2:us-east-1:1:a")
    second = finding("dup", arn="arn:aws:ec2:us-east-1:1:b")
    forward = make_plan([second, first], [])
    backward = make_plan([first, second], [])
    assert [c["arn"] for c in forward["candidates"]] == [first.arn, second.arn]
    assert plan_bytes(forward) == plan_bytes(backward)
