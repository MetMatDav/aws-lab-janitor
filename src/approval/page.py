"""Server-rendered HTML for the approval link: no script, no external resource.

Every dynamic value goes through ``html.escape``. The form has no ``action``,
so it posts back to the page's own URL (the CSP allows ``form-action 'self'``).
"""

from html import escape

from shared.approvals import ApprovalRecord

_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:52rem;margin:2rem auto;padding:0 1rem;"
    "line-height:1.5;color:#1b1b1b;background:#fff}"
    "pre{background:#f4f4f4;padding:1rem;overflow-x:auto;white-space:pre-wrap}"
    "dt{font-weight:600}dd{margin:0 0 .5rem 0;word-break:break-all}"
    "input{font:inherit;padding:.3rem;width:6rem}"
    "button{font:inherit;padding:.4rem 1rem;margin-right:.5rem;cursor:pointer}"
    "button[value=reject]{background:#fff;border:1px solid #888}"
    "button[value=approve]{background:#b3261e;color:#fff;border:1px solid #b3261e}"
)


def _page(title: str, content: str) -> str:
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="robots" content="noindex">\n'
        f"<title>{escape(title)} - Lab Janitor</title>\n"
        f"<style>{_STYLE}</style>\n"
        f"</head>\n<body>\n<h1>{escape(title)}</h1>\n{content}\n</body>\n</html>\n"
    )


def render_form(
    record: ApprovalRecord,
    summary_text: str,
    confirmation_hint: str = "last 4 characters of the run id",
) -> str:
    """The review page: plan facts, the candidate list and the decision form."""
    summary = record.plan_summary
    headline = (
        f"Lab Janitor wants to delete {summary['candidateCount']} resources "
        f"(~${summary['estimatedMonthlyUsd']:.2f}/month)."
    )
    content = (
        f"<p>{escape(headline)}</p>\n"
        "<dl>\n"
        f"<dt>Run id</dt><dd>{escape(record.run_id)}</dd>\n"
        f"<dt>Plan sha256</dt><dd>{escape(record.plan_sha256)}</dd>\n"
        f"<dt>Expires at (UTC)</dt><dd>{escape(record.expires_at)}</dd>\n"
        "</dl>\n"
        f"<pre>{escape(summary_text)}</pre>\n"
        '<form method="post">\n'
        f'<p><label for="confirm">Type the {escape(confirmation_hint)} to confirm:</label>\n'
        '<input id="confirm" name="confirm" type="text" required autocomplete="off"'
        ' spellcheck="false"></p>\n'
        '<p><button type="submit" name="decision" value="approve">Approve deletion</button>\n'
        '<button type="submit" name="decision" value="reject">Reject</button></p>\n'
        "</form>"
    )
    return _page("Approve cleanup plan", content)


def render_status(title: str, text: str) -> str:
    """A page with a title and one paragraph, for errors and closed approvals."""
    return _page(title, f"<p>{escape(text)}</p>")


def render_done(decision: str) -> str:
    """The confirmation page after a decision was delivered."""
    if decision == "approve":
        return render_status(
            "Plan approved", "The janitor will now delete the resources in the plan."
        )
    return render_status("Plan rejected", "Nothing will be deleted for this run.")
