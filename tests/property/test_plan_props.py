"""Property tests for the canonical plan: ``shared/plan.py`` and ``shared/findings.py``."""

import dataclasses
import json
from datetime import UTC
from fractions import Fraction

from hypothesis import given
from hypothesis import strategies as st

from shared.findings import KINDS, ErrorFinding, Finding
from shared.plan import build_plan, plan_bytes, plan_sha256
from shared.settings import Settings

RUN_ID = "20261102T120000Z-abc1"
REGIONS = ["us-east-1", "eu-west-1", "global"]
SETTINGS = Settings(
    dry_run=True,
    min_age_days=7,
    name_prefix="LAB-",
    tag_key="Purpose",
    tag_value="lab",
    keep_tag_key="janitor",
    keep_tag_value="keep",
    plans_bucket="plans",
    audit_table="audit",
    reports_topic_arn="arn:aws:sns:us-east-1:123456789012:reports",
    region="us-east-1",
)

# Small pools so that generated findings often share region, kind, resource id and arn.
regions = st.sampled_from(REGIONS)
kinds = st.sampled_from(sorted(KINDS))
resource_ids = st.sampled_from(["vol-1", "vol-2", "snap-1", "LAB-x"])
arns = st.sampled_from([None, "arn:aws:ec2:us-east-1:123456789012:volume/vol-1", ""])
small_text = st.text(max_size=8)
costs = st.none() | st.floats(min_value=0, max_value=1e5, allow_nan=False, allow_infinity=False)
tags = st.lists(st.fixed_dictionaries({"Key": small_text, "Value": small_text}), max_size=2)

findings = st.builds(
    Finding,
    region=regions,
    kind=kinds,
    resource_id=resource_ids,
    arn=arns,
    detail=small_text,
    lab=st.booleans(),
    candidate=st.booleans(),
    reason=small_text,
    age_days=st.none() | st.integers(-5, 4000),
    estimated_monthly_usd=costs,
    eligible=st.booleans(),
    tags=tags,
)
errors = st.builds(
    ErrorFinding,
    region=regions,
    service=st.sampled_from(["ec2:DescribeVolumes", "rds:DescribeDBInstances"]),
    error=small_text,
    classification=st.sampled_from(["access-denied", "throttled", "other"]),
)
generated_at = st.datetimes(timezones=st.just(UTC))


@st.composite
def with_duplicates(draw, elements):
    """A list of ``elements`` plus exact copies of some of its members."""
    base = draw(st.lists(elements, max_size=8))
    if not base:
        return base
    copies = draw(st.lists(st.sampled_from(base), max_size=3))
    return base + [dataclasses.replace(item) for item in copies]


def _plan(found, errs, when):
    return build_plan(RUN_ID, SETTINGS, found, errs, REGIONS, when)


@given(st.data(), with_duplicates(findings), with_duplicates(errors), generated_at)
def test_plan_bytes_do_not_depend_on_input_order(data, found, errs, when):
    """Fails if build_plan stops sorting findings or errors by a total key (e.g. drops the
    whole-finding tie-breaker), so two orders of the same input give different bytes."""
    shuffled_findings = data.draw(st.permutations(found))
    shuffled_errors = data.draw(st.permutations(errs))
    assert plan_bytes(_plan(found, errs, when)) == plan_bytes(
        _plan(shuffled_findings, shuffled_errors, when)
    )


@given(with_duplicates(findings), with_duplicates(errors), generated_at)
def test_plan_bytes_round_trip_through_json(found, errs, when):
    """Fails if plan_bytes stops being canonical (unsorted keys, extra whitespace) or the
    plan holds a value JSON cannot reproduce exactly."""
    data = plan_bytes(_plan(found, errs, when))
    assert plan_bytes(json.loads(data)) == data


# (field, strategy of a replacement value) for every field to_dict publishes.
FIELD_VALUES = {
    "region": regions | small_text,
    "kind": kinds,
    "resource_id": resource_ids | small_text,
    "arn": arns | small_text,
    "detail": small_text,
    "lab": st.booleans(),
    "candidate": st.booleans(),
    "reason": small_text,
    "age_days": st.none() | st.integers(-5, 4000),
    "estimated_monthly_usd": costs,
    "eligible": st.booleans(),
}


@given(
    st.data(),
    st.lists(findings, min_size=1, max_size=6),
    st.lists(errors, max_size=3),
    generated_at,
)
def test_changing_one_field_of_one_finding_changes_the_sha256(data, found, errs, when):
    """Fails if a published finding field stops reaching the plan bytes (e.g. to_dict drops
    ``eligible``), so an edited plan would keep the approved hash."""
    index = data.draw(st.integers(0, len(found) - 1))
    name = data.draw(st.sampled_from(sorted(FIELD_VALUES)))
    old = getattr(found[index], name)
    new = data.draw(FIELD_VALUES[name].filter(lambda value: repr(value) != repr(old)))
    changed = list(found)
    changed[index] = dataclasses.replace(found[index], **{name: new})
    before = plan_sha256(plan_bytes(_plan(found, errs, when)))
    after = plan_sha256(plan_bytes(_plan(changed, errs, when)))
    assert before != after


@given(st.data(), with_duplicates(findings))
def test_total_is_the_rounded_exact_sum_in_any_order(data, found):
    """Fails if the total is summed naively (order-dependent float error), skips a cost,
    or is not rounded to cents."""
    shuffled = data.draw(st.permutations(found))
    known = [f.estimated_monthly_usd for f in found if f.estimated_monthly_usd is not None]
    exact = float(sum((Fraction(cost) for cost in known), Fraction(0)))
    plan = build_plan(RUN_ID, SETTINGS, shuffled, [], REGIONS, data.draw(generated_at))
    assert plan["estimatedMonthlyUsd"] == round(exact, 2)
    assert isinstance(plan["estimatedMonthlyUsd"], float)
