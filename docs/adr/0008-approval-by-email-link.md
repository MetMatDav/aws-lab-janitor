# 0008. Approval by a link in an email

## Status

Accepted on 2026-11-02, with the IAM route built in the same release.

## Context

The approver is one person who reads the weekly email on a phone. An approval
step that needs a console login, a CLI or a VPN will be skipped, and a skipped
approval means the janitor never deletes anything. But a link that approves
when it is opened will be opened by a mail scanner before any person sees it.

## Decision

The approval request is an email with one link to `/approvals/<id>`. Opening
it (`GET`) only shows the plan summary: nothing is written. Deciding is a form
`POST` that must carry the last four characters of the run id, typed by the
person. The page has no script and a policy that forbids any. Knowing the id is
what authorizes the request, so the id is a bearer credential: the approvals
topic accepts publishes only from the notifier and new subscriptions only from
the deployment role and the account root. A second route, `/iam/approvals/<id>`,
authenticates the caller with SigV4 and compares it with a configured role;
`approvalAuth` selects link, IAM or both.

## Consequences

- Approving takes a tap, four characters and a button, from any device.
- A scanner that follows the link changes nothing. One that submits forms
  would still need the code, which is on the page: the code stops accidents,
  not attackers. It is not a secret.
- Possession of the mailbox is the authority to approve. Forwarding the email
  delegates it. Whoever can read the id elsewhere (the audit table, CloudTrail,
  the execution history) can approve too:
  [known limitations](../known-limitations.md), section 1.
- The IAM route removes that weakness and costs the convenience. It has not
  been exercised against the live API yet.

## Where to look

- [src/approval/handler.py](../../src/approval/handler.py),
  [src/approval/decision.py](../../src/approval/decision.py),
  [src/approval/page.py](../../src/approval/page.py).
- [lab_janitor/notifications.py](../../lab_janitor/notifications.py): the
  approvals topic policy.
- [tests/unit/test_approval_handler.py](../../tests/unit/test_approval_handler.py):
  `test_get_renders_form_and_writes_nothing`,
  `test_post_wrong_confirmation_is_400_without_any_write`,
  `test_iam_route_rejects_other_principals_before_any_write`.
- [tests/unit/test_approval_page.py](../../tests/unit/test_approval_page.py):
  `test_form_has_no_script_and_escapes_summary`.
- [docs/threat-model.md](../threat-model.md).
