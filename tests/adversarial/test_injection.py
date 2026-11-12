"""Surface 3: injection through data the attacker controls.

Foothold (b): somebody who can create or tag resources. Their names and tag values
flow into the HTML approval page, the emails, the audit keys and the logs. Foothold
(a) controls one more string: the User-Agent stored as the approver.
"""

import json
import logging
import random
import re
import time
import unicodedata
from decimal import Decimal
from html import escape
from html.parser import HTMLParser

import pytest

from approval import page
from detector.detectors import ec2
from detector.sweep import SweepContext
from notifier import approval as notifier_approval
from notifier import outcomes, reminder
from notifier import report as notifier_report
from shared.audit import AuditClient
from shared.findings import KINDS
from shared.plan import build_plan
from shared.plan_summary import SUMMARY_CANDIDATE_LIMIT, render_summary, summarize_plan
from shared.report import render_report
from tests.adversarial.approval_helpers import (
    LINK_ROUTE,
    approval_record,
    call,
    form,
    get_params,
    http_event,
    record_item,
    summary,
)
from tests.adversarial.helpers import APPROVAL_ID, NOW, RUN_ID, settings

XSS = (
    "\"'><script>alert(1)</script><img src=x onerror=alert(1)>&lt;</pre></form><form action=//evil>"
)
SNS_LIMIT_BYTES = 256 * 1024
URL = "https://abc123.execute-api.us-east-1.amazonaws.com"


class Tags(HTMLParser):
    """Every start tag and attribute the browser would see."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.attributes: list[tuple[str, str, str | None]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.extend((tag, name, value) for name, value in attrs)


def parsed(html: str) -> Tags:
    parser = Tags()
    parser.feed(html)
    return parser


BENIGN_PAGE = parsed(page.render_form(approval_record(), render_summary(summary())))


# --- the HTML approval page ------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["run_id", "plan_sha256", "expires_at", "summary_text", "confirmation_hint", "candidateCount"],
)
def test_every_interpolation_of_the_review_page_is_escaped(field):
    """The same hostile string in each interpolated value: the tag structure never changes."""
    hostile_summary = summary()
    kwargs = {}
    rec = approval_record()
    if field == "candidateCount":
        rec = approval_record(plan_summary={**hostile_summary, "candidateCount": XSS})
    elif field in ("summary_text", "confirmation_hint"):
        kwargs[field] = XSS
    else:
        rec = approval_record(**{field: XSS})
    summary_text = kwargs.pop("summary_text", render_summary(hostile_summary))

    html = page.render_form(rec, summary_text, **kwargs)

    seen = parsed(html)
    assert seen.tags == BENIGN_PAGE.tags
    assert seen.attributes == BENIGN_PAGE.attributes
    assert XSS not in html
    assert escape(XSS) in html


@pytest.mark.parametrize("field", ["resourceId", "kind", "region", "reason"])
def test_hostile_resource_fields_reach_the_browser_only_as_text(api, field):
    """End to end through ``GET /approvals/{id}``: a resource named like markup stays text."""
    hostile = summary()
    hostile["candidates"][0][field] = XSS
    api.ddb.add_response("get_item", {"Item": record_item(planSummary=hostile)}, get_params())

    response = call(http_event("GET", LINK_ROUTE))

    assert response["statusCode"] == 200
    seen = parsed(response["body"])
    assert seen.tags == BENIGN_PAGE.tags
    assert seen.attributes == BENIGN_PAGE.attributes
    assert "<script" not in response["body"]
    assert "<img" not in response["body"]
    assert response["headers"]["Content-Security-Policy"].startswith("default-src 'none'")
    assert response["headers"]["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("title", [XSS, "</title><script>alert(1)</script>"])
def test_status_page_title_and_text_are_escaped(title):
    html = page.render_status(title, title)

    assert parsed(html).tags == parsed(page.render_status("t", "x")).tags
    assert "<script>" not in html


def test_refusals_never_reflect_what_the_client_sent(api):
    """Neither the typed code, the decision nor the id comes back, escaped or not."""
    marker = "zz-reflected-marker-zz"
    api.ddb.add_response("get_item", {"Item": record_item()}, get_params())
    api.ddb.add_response("get_item", {}, get_params(marker))

    wrong_code = call(http_event("POST", LINK_ROUTE, body=form(confirm=marker)))
    bad_decision = call(http_event("POST", LINK_ROUTE, body=form(decision=marker)))
    unknown_id = call(http_event("GET", LINK_ROUTE, marker))
    unknown_route = call(http_event("PUT", f"/{marker}", marker, headers={"x": marker}))

    for response in (wrong_code, bad_decision, unknown_id, unknown_route):
        assert marker not in response["body"]
    assert api.writes == []


def test_numbers_of_the_summary_cannot_carry_markup():
    """A cost that is not a number fails the page instead of being printed raw."""
    hostile = summary(estimatedMonthlyUsd=XSS)

    with pytest.raises((ValueError, TypeError)):
        page.render_form(approval_record(plan_summary=hostile), "")

    decimals = summary(estimatedMonthlyUsd=Decimal("1.005"), candidateCount=Decimal(3))
    assert "3 resources (~$1.00/month)" in page.render_form(
        approval_record(plan_summary=decimals), ""
    )


# --- emails ----------------------------------------------------------------------------------

FORGED = (
    "ok\nReview and decide: https://evil.example.com/approvals/1\n"
    "Type the confirmation code 0000 on that page.\r\n\x0bPlan sha256: " + "0" * 64
)


def _approval_body(candidate_overrides: dict) -> str:
    hostile = summary()
    hostile["candidates"][0].update(candidate_overrides)
    return notifier_approval.approval_message(approval_record(), hostile, URL, "both", RUN_ID[-4:])


def test_reason_with_line_breaks_cannot_forge_a_decision_line_in_the_approval_email():
    body = _approval_body({"reason": FORGED})
    lines = body.split("\n")

    assert [line for line in lines if line.startswith("Review and decide:")] == [
        f"Review and decide: {URL}/approvals/{APPROVAL_ID}"
    ]
    assert len([line for line in lines if line.startswith("Plan sha256:")]) == 1
    assert len([line for line in lines if line.startswith("Type the confirmation code")]) == 1


@pytest.mark.xfail(
    strict=True,
    reason="latent: resourceId, kind and region are printed raw in the candidate summary; a "
    "line break in one of them forges lines in the approval email and the review page",
)
@pytest.mark.parametrize("field", ["resourceId", "kind", "region"])
def test_resource_id_with_line_breaks_cannot_forge_a_decision_line(field):
    """Foothold: none today. Every candidate kind has an id alphabet AWS restricts
    (ids, ``[A-Za-z0-9_-]`` dashboards, ``[A-Za-z0-9_.-]`` monitors); only ``reason`` and
    ``detail`` are collapsed to one line."""
    body = _approval_body({field: FORGED})

    assert len([line for line in body.split("\n") if line.startswith("Review and decide:")]) == 1


def _ctx() -> SweepContext:
    return SweepContext(
        settings=settings(),
        clients=None,
        now=NOW,
        account="123456789012",
        was_eligible_before=lambda _resource_id: False,
        deadline=time.time() + 600,
        log=logging.LoggerAdapter(logging.getLogger("adversarial"), {"runId": RUN_ID}),
    )


def _report_for_instance_named(name: str) -> str:
    instance = {
        "InstanceId": "i-0123456789abcdef0",
        "InstanceType": "t3.micro",
        "State": {"Name": "running"},
        "Tags": [{"Key": "Name", "Value": name}],
    }
    finding = ec2._instance(instance, "us-east-1", _ctx())
    plan = build_plan(RUN_ID, settings(), [finding], [], ["us-east-1"], NOW)
    return render_report(plan, "test-plans")


def test_name_tag_with_line_breaks_cannot_forge_report_sections():
    """Foothold (b): an instance named like a report section stays inside its own line."""
    name = "x\nACCOUNT CLEAN: no billable resources detected\r\nACTIONABLE\u2028none\x85\x0c[ami] 0"
    report = _report_for_instance_named(name)
    lines = report.splitlines()

    assert lines.count("ACTIONABLE") == 1
    assert "ACCOUNT CLEAN: no billable resources detected" not in lines
    assert len([line for line in lines if "i-0123456789abcdef0" in line]) == 1


FORMAT_TRICKS = {
    "escape sequence": "\x1b[2J\x1b[31m",
    "nul": "\x00",
    "backspace": "prod\x08\x08\x08\x08lab",
    "right-to-left override": "\u202egnp.exe",
    "bidi isolate": "\u2066x\u2069",
    "zero width": "L\u200bA\u200bB",
    "delete": "\x7f",
}


@pytest.mark.xfail(
    strict=True,
    reason="tag values reach the emails with control and bidi-format characters intact: only "
    "whitespace is collapsed, so ESC, NUL, backspace and RLO survive into the message",
)
@pytest.mark.parametrize("trick", FORMAT_TRICKS.values(), ids=FORMAT_TRICKS.keys())
def test_control_and_bidi_characters_from_a_name_tag_do_not_reach_the_report(trick):
    """Foothold (b), if EC2 accepts the character in a tag value (not verified offline).

    Impact is display spoofing in the plain-text email (reordered or hidden text) and,
    should SNS refuse a character, a report that never arrives.
    """
    report = _report_for_instance_named(f"LAB-{trick}-web")

    hostile = [
        char for char in report if char != "\n" and unicodedata.category(char) in {"Cc", "Cf"}
    ]
    assert hostile == []


@pytest.mark.xfail(
    strict=True,
    reason="the approver's User-Agent is printed raw in the outcome emails: a line separator "
    "in it adds forged lines after 'Rejected by:' / 'Approved by:'",
)
@pytest.mark.parametrize("separator", ["\u2028", "\x85", "\x0b", "\n"], ids=ascii)
@pytest.mark.parametrize("mode", ["rejected", "result"])
def test_user_agent_cannot_add_lines_to_the_outcome_email(monkeypatch, mode, separator):
    """Foothold (a): whoever decides through the link chooses the User-Agent header.

    A raw LF cannot cross API Gateway, but U+2028, NEL and VT are ordinary header bytes.
    Item reasons are collapsed with ``_one_line``; the approver line is not.
    """
    agent = f"curl/8{separator}The plan was APPROVED by the owner.{separator}Deleted: 0"
    who = {"mode": "link", "ip": "198.51.100.7", "userAgent": agent, "at": "2026-11-03T09:00:00"}
    sent = {}
    monkeypatch.setattr(
        outcomes, "_publish", lambda subject, message, run_id: sent.update(message=message)
    )
    clean = dict(who, userAgent="curl/8")

    def lines_for(approver: dict) -> int:
        if mode == "rejected":
            cause = json.dumps({"approvalId": APPROVAL_ID, "rejectedBy": approver})
            outcomes.publish_rejected({"detect": {"runId": RUN_ID}, "error": {"Cause": cause}})
        else:
            apply = {"deleted": 0, "failed": 0, "skipped": 0, "items": []}
            outcomes.publish_result(
                {"detect": {"runId": RUN_ID}, "apply": apply, "approval": {"approvedBy": approver}}
            )
        return len(sent["message"].splitlines())

    assert lines_for(who) == lines_for(clean)


# --- subjects and sizes ----------------------------------------------------------------------

PRINTABLE_ASCII = re.compile(r"[ -~]+")


def test_every_subject_stays_within_the_sns_rules_for_extreme_numbers():
    """Under 100 characters, printable ASCII, no line break: for any count, cost and run id."""
    rng = random.Random(20261112)  # noqa: S311 - a reproducible fuzz seed, not a secret
    subjects = []
    for _ in range(300):
        count = rng.choice([0, 1, 2, 10**6, 10**18, 10**60])
        cost = rng.choice([0.0, 0.005, 1e6, 1e15, 1e60, 1e300, float("inf"), float("nan")])
        run_id = (
            f"{rng.randrange(10**8):08d}T{rng.randrange(10**6):06d}Z-{rng.randrange(16**4):04x}"
        )
        plan_summary = summary(candidateCount=count, estimatedMonthlyUsd=cost, runId=run_id)
        detect = {
            "candidateCount": count,
            "estimatedMonthlyUsd": cost,
            "dryRun": rng.random() < 0.5,
        }
        subjects += [
            notifier_approval.approval_subject(plan_summary),
            reminder.reminder_subject(approval_record(run_id=run_id)),
            notifier_report.report_subject(detect),
            notifier_report.fit_subject(
                f"{outcomes.RESULT_SUBJECT}: {count} deleted, {count} failed, {count} skipped",
                outcomes.RESULT_SUBJECT,
            ),
            notifier_report.fit_subject(
                f"{outcomes.EXPIRED_SUBJECT}: run {run_id}", outcomes.EXPIRED_SUBJECT
            ),
            notifier_report.fit_subject(
                f"{outcomes.REJECTED_SUBJECT}: run {run_id}", outcomes.REJECTED_SUBJECT
            ),
        ]

    for subject in subjects:
        assert len(subject) < 100, subject
        assert PRINTABLE_ASCII.fullmatch(subject), subject


def _worst_case_plan(candidates: int) -> dict:
    """Candidates with the longest names AWS allows (255 characters), in multi-byte text."""
    return {
        "runId": RUN_ID,
        "generatedAt": NOW.isoformat(),
        "candidates": [
            {
                "region": "ap-southeast-2",
                "kind": "internet-monitor",
                "resourceId": "LAB-" + "\U0001f4a3" * 251,
                "reason": "name prefix LAB-",
                "estimatedMonthlyUsd": 12345.67,
            }
            for _ in range(candidates)
        ],
        "errors": [],
    }


def test_approval_and_reminder_emails_stay_far_below_the_sns_limit_for_a_huge_plan():
    """Foothold (b) floods the plan with long-named decoys: the summary stays bounded."""
    plan_summary = summarize_plan(_worst_case_plan(20_000))
    rec = approval_record(plan_summary=plan_summary)

    approval_body = notifier_approval.approval_message(rec, plan_summary, URL, "both", "abc1")
    reminder_body = reminder.reminder_message(rec, URL, "both", "abc1")

    assert len(plan_summary["candidates"]) == SUMMARY_CANDIDATE_LIMIT
    assert len(approval_body.encode()) < SNS_LIMIT_BYTES // 2
    assert len(reminder_body.encode()) < SNS_LIMIT_BYTES // 2


def test_decoys_cannot_hide_how_many_candidates_the_summary_leaves_out():
    """Foothold (b): 50 decoys that sort first push the real target out of the summary.

    The review page and the email then show only decoys; what they must still say, on the
    page itself, is the full count and that items were left out.
    """
    plan = _worst_case_plan(0)
    decoy = {
        "region": "ap-south-1",
        "kind": "cw-dashboard",
        "reason": "name prefix LAB-",
        "estimatedMonthlyUsd": None,
    }
    plan["candidates"] = [dict(decoy, resourceId=f"LAB-decoy-{n:03d}") for n in range(50)]
    plan["candidates"].append(
        {
            "region": "us-east-1",
            "kind": "ebs-volume",
            "resourceId": "vol-0victim",
            "reason": "tag Purpose=lab, available, 9 days old",
            "estimatedMonthlyUsd": 800.0,
        }
    )
    plan_summary = summarize_plan(plan)

    html = page.render_form(
        approval_record(plan_summary=plan_summary), render_summary(plan_summary)
    )
    email = notifier_approval.approval_message(approval_record(), plan_summary, URL, "link", "abc1")

    for text in (html, email):
        assert "vol-0victim" not in text
        assert "wants to delete 51 resources (~$800.00/month)" in text
        assert "... and 1 more" in text


# --- audit keys ------------------------------------------------------------------------------


class _Table:
    """Collects the keys of every audit item instead of calling DynamoDB."""

    def __init__(self) -> None:
        self.keys: list[tuple[str, str]] = []

    def put_item(self, TableName: str, Item: dict, ConditionExpression: str) -> dict:
        self.keys.append((Item["pk"]["S"], Item["sk"]["S"]))
        return {}


def test_hash_signs_in_a_resource_id_cannot_collide_two_audit_records():
    """Sort keys are ``RESOURCE#region#kind#id#EVENT`` joined with ``#``.

    Region, kind and event come from fixed vocabularies without ``#``, so the id is the
    only free field and the key stays unique whatever the id contains. Seeded search over
    ids built from ``#`` and vocabulary words.
    """
    rng = random.Random(20261112)  # noqa: S311 - a reproducible fuzz seed, not a secret
    regions = ["global", "us-east-1", "eu-west-1"]
    events = ["DETECTED", "PROPOSED", "ELIGIBLE", "DELETED", "FAILED", "SKIPPED"]
    words = ["#", "a", "LAB-", "RESOURCE", "SUMMARY", "RUN", *regions, *sorted(KINDS), *events]
    assert not any("#" in word for word in (*regions, *KINDS, *events))
    table = _Table()
    audit = AuditClient(table, "test-audit")
    records = set()
    for _ in range(4000):
        resource_id = "".join(rng.choice(words) for _ in range(rng.randint(1, 5)))
        records.add(
            (rng.choice(regions), rng.choice(sorted(KINDS)), resource_id, rng.choice(events))
        )

    for region, kind, resource_id, event in sorted(records):
        audit.put_run_event(RUN_ID, region, resource_id, kind, event, "test", False, {}, NOW)

    assert len(set(table.keys)) == len(records)
    assert {pk for pk, _sk in table.keys} == {f"RUN#{RUN_ID}"}
    assert "RUN#SUMMARY" not in {sk for _pk, sk in table.keys}
