from datetime import UTC, datetime

import pytest

from shared.findings import ErrorFinding, Finding
from shared.plan import build_plan
from shared.report import render_report, truncate_report
from shared.settings import Settings

NOW = datetime(2026, 11, 10, 14, 0, 12, tzinfo=UTC)
RUN_ID = "20261110T140012Z-7f3a"
BUCKET = "plans-bucket"
REGIONS = ["us-east-1", "us-west-2", "eu-west-1", "ap-south-1"]
SETTINGS = Settings(
    dry_run=True,
    min_age_days=7,
    name_prefix="lab-",
    tag_key="env",
    tag_value="lab",
    keep_tag_key="janitor",
    keep_tag_value="keep",
    plans_bucket=BUCKET,
    audit_table="audit",
    reports_topic_arn="arn:aws:sns:us-east-1:123456789012:reports",
    region="us-east-1",
)


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


def report(findings=(), errors=(), regions=REGIONS, settings=SETTINGS) -> str:
    plan = build_plan(RUN_ID, settings, list(findings), list(errors), list(regions), NOW)
    return render_report(plan, BUCKET)


def section(text: str, title: str) -> list[str]:
    """Lines under the heading ``title`` up to the next blank line."""
    lines = text.split("\n")
    start = lines.index(title) + 1
    end = lines.index("", start) if "" in lines[start:] else len(lines)
    return lines[start:end]


def test_header_lines_in_order():
    text = report(
        [
            finding("nat-1"),
            finding("vol-1", kind="ebs-volume", candidate=False, estimated_monthly_usd=0.8),
        ]
    )
    assert text.split("\n")[:8] == [
        "LAB JANITOR WEEKLY REPORT",
        f"Run: {RUN_ID}",
        "Mode: DRY RUN",
        "Regions swept: 4",
        "Findings: 2",
        "Candidates for deletion: 1",
        "Estimated fixed monthly cost of everything detected: ~$33.65",
        "",
    ]


def test_live_mode_header():
    live = Settings(**{**SETTINGS.__dict__, "dry_run": False})
    assert "Mode: LIVE" in report(settings=live).split("\n")


def test_sections_in_order_and_footer():
    text = report(
        [finding("nat-1"), finding("vol-1", kind="ebs-volume", candidate=False)],
        [ErrorFinding("eu-west-1", "rds:DescribeDBInstances", "denied", "access-denied")],
    )
    lines = text.split("\n")
    order = [
        lines.index(t) for t in ("ACTIONABLE", "INVENTORY", "ERRORS", "REGIONS WITH NOTHING FOUND")
    ]
    assert order == sorted(order)
    assert lines[-1] == f"Full plan: s3://{BUCKET}/plans/{RUN_ID}/plan.json"


def test_account_clean_only_without_errors():
    clean = report()
    assert "ACCOUNT CLEAN: no billable resources detected" in clean.split("\n")
    assert "ERRORS" not in clean.split("\n")

    error = ErrorFinding("eu-west-1", "rds:DescribeDBInstances", "denied", "access-denied")
    assert "ACCOUNT CLEAN" not in report(errors=[error])
    assert "ACCOUNT CLEAN" not in report([finding("vol-1", candidate=False)])


def test_candidates_section_lists_reason_and_cost():
    text = report([finding("nat-1", reason="lab-tag-and-age")])
    assert section(text, "ACTIONABLE") == [
        "[nat-gateway] nat-1 (us-east-1) 12d lab-tag-and-age ~$32.85/mo"
    ]


def test_candidate_without_age_or_cost_omits_those_tokens():
    text = report(
        [finding("snap-1", kind="ebs-snapshot", age_days=None, estimated_monthly_usd=None)]
    )
    assert section(text, "ACTIONABLE") == ["[ebs-snapshot] snap-1 (us-east-1) lab-tag"]


def test_inventory_grouped_by_kind():
    text = report(
        [
            finding(
                "vol-2",
                kind="ebs-volume",
                candidate=False,
                region="us-west-2",
                detail="gp3 8 GiB",
                estimated_monthly_usd=0.64,
                age_days=3,
            ),
            finding(
                "dash-1",
                region="global",
                kind="cw-dashboard",
                candidate=False,
                detail="dashboard",
                estimated_monthly_usd=None,
                age_days=None,
            ),
            finding(
                "vol-1",
                kind="ebs-volume",
                candidate=False,
                detail="gp3 10 GiB",
                estimated_monthly_usd=0.8,
                age_days=40,
            ),
        ]
    )
    assert section(text, "INVENTORY") == [
        "[cw-dashboard] 1 resources",
        "  dash-1 (global) dashboard",
        "[ebs-volume] 2 resources",
        "  vol-1 (us-east-1) 40d ~$0.80/mo gp3 10 GiB",
        "  vol-2 (us-west-2) 3d ~$0.64/mo gp3 8 GiB",
    ]


def test_errors_single_and_collapsed():
    text = report(
        errors=[
            ErrorFinding("eu-west-1", "ec2:DescribeInstances", "denied one", "access-denied"),
            ErrorFinding("eu-west-1", "ec2:DescribeVolumes", "denied two", "access-denied"),
            ErrorFinding("eu-west-1", "rds:DescribeDBInstances", "denied rds", "access-denied"),
            ErrorFinding("ap-south-1", "ec2:DescribeAddresses", "boom", "other"),
        ]
    )
    assert section(text, "ERRORS") == [
        "ap-south-1 ec2:DescribeAddresses other: boom",
        "eu-west-1 ec2 access-denied: 2 occurrences, first message: denied one",
        "eu-west-1 rds:DescribeDBInstances access-denied: denied rds",
    ]


def test_regions_with_nothing_found():
    text = report(
        [finding("nat-1"), finding("dash-1", region="global", kind="cw-dashboard")],
        [ErrorFinding("ap-south-1", "ec2:DescribeAddresses", "boom", "other")],
    )
    assert section(text, "REGIONS WITH NOTHING FOUND") == ["us-west-2, eu-west-1"]


def test_regions_with_nothing_found_none():
    text = report([finding(f"nat-{r}", region=r) for r in REGIONS])
    assert section(text, "REGIONS WITH NOTHING FOUND") == ["none"]


def test_report_has_no_em_dashes():
    text = report(
        [finding("nat-1"), finding("vol-1", candidate=False)],
        [ErrorFinding("eu-west-1", "ec2:DescribeVolumes", "x", "other")],
    )
    assert "—" not in text


def test_truncate_returns_text_unchanged_when_it_fits():
    assert truncate_report("a\nb", 3, "s3://b/k") == "a\nb"


def test_truncate_keeps_whole_lines_under_limit():
    text = "\n".join(f"line {i}" for i in range(50_000))
    out = truncate_report(text, 150_000, "s3://b/k")
    assert len(out.encode()) <= 150_000 and out.endswith("Full report: s3://b/k")
    kept = out.split("\n... truncated")[0].split("\n")
    assert kept == text.split("\n")[: len(kept)]
    omitted = 50_000 - len(kept)
    assert out.endswith(f"\n... truncated, {omitted} lines omitted. Full report: s3://b/k")


def test_truncate_drops_lines_to_make_room_for_suffix():
    text = "aaaa\nbbbb\n" + "c" * 100
    suffix = "\n... truncated, 1 lines omitted. Full report: p"
    limit = len("aaaa\nbbbb") + len(suffix)
    assert truncate_report(text, limit, "p") == "aaaa\nbbbb" + suffix
    shorter = truncate_report(text, limit - 1, "p")
    assert shorter == "aaaa\n... truncated, 2 lines omitted. Full report: p"
    assert len(shorter.encode()) <= limit - 1


def test_truncate_counts_utf8_bytes():
    text = "\u00e9" * 10 + "\n" + "x" * 100  # first line: 10 characters, 20 bytes
    suffix = "\n... truncated, 1 lines omitted. Full report: p"
    fits_by_chars_only = 10 + len(suffix) + 5
    out = truncate_report(text, fits_by_chars_only, "p")
    assert out == "\n... truncated, 2 lines omitted. Full report: p"
    assert truncate_report(text, 20 + len(suffix), "p") == "\u00e9" * 10 + suffix


def test_truncate_raises_when_suffix_cannot_fit():
    with pytest.raises(ValueError):
        truncate_report("x" * 100, 10, "s3://b/k")
