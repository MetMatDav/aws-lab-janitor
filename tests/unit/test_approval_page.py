from approval import page
from shared.approvals import ApprovalRecord

RECORD = ApprovalRecord(
    approval_id="11111111-aaaa-bbbb-cccc-222222222222",
    run_id="20261102T120000Z-abc1",
    plan_version_id="plan-v1",
    plan_sha256="ab" * 32,
    plan_summary={"candidateCount": 1, "estimatedMonthlyUsd": 32.85},
    status="PENDING",
    created_at="2026-11-03T09:00:00+00:00",
    expires_at="2026-11-06T08:30:00+00:00",
    approved_by=None,
    decided_at=None,
)


def test_form_has_no_script_and_escapes_summary():
    html = page.render_form(RECORD, "[ec2] <b>i-1</b> (us-east-1) idle & old")

    assert "<script" not in html.lower()
    assert "<b>" not in html
    assert "&lt;b&gt;i-1&lt;/b&gt;" in html
    assert "idle &amp; old" in html
    assert "http://" not in html
    assert "https://" not in html
    assert "<img" not in html.lower()


def test_form_shows_run_sha_expiry_and_summary_in_pre():
    html = page.render_form(RECORD, "line one\nline two")

    assert RECORD.run_id in html
    assert RECORD.plan_sha256 in html
    assert RECORD.expires_at in html
    assert "<pre>line one\nline two</pre>" in html
    assert "last 4 characters of the run id" in html
    assert RECORD.approval_id not in html


def test_form_posts_to_self_with_confirm_field():
    html = page.render_form(RECORD, "")

    assert html.count("<form") == 1
    assert '<form method="post">' in html
    assert "action=" not in html
    assert 'name="confirm"' in html
    assert '<button type="submit" name="decision" value="approve">' in html
    assert '<button type="submit" name="decision" value="reject">' in html


def test_form_accepts_a_custom_confirmation_hint():
    html = page.render_form(RECORD, "", confirmation_hint="the <code>")

    assert "the &lt;code&gt;" in html


def test_status_page_escapes_title_and_text():
    html = page.render_status("Gone <now>", "Text & more")

    assert "Gone &lt;now&gt;" in html
    assert "Text &amp; more" in html
    assert "<form" not in html
    assert "<script" not in html.lower()


def test_done_page_names_the_decision():
    assert "approved" in page.render_done("approve").lower()
    assert "rejected" in page.render_done("reject").lower()
    assert "<form" not in page.render_done("approve")
