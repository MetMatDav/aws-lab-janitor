# Security audit, 2026-11-18

An independent pass over the repository before publication, run with
[cloudflare/security-audit-skill](https://github.com/cloudflare/security-audit-skill)
(commit `c1c8a8c`) on commit `ba4f8ad`.

## How it ran

- Profile `quick`: one wave of four hunters, one coverage critic, one
  independent verifier per surviving candidate. Eleven agent runs in all.
- Source and local only. The agents read the repository read-only. Target code
  ran inside an OS sandbox with no network, an empty environment, a read-only
  tree and resource limits. No AWS account, credential or deployed endpoint was
  touched.
- A `quick` run is a partial pass by definition and claims no complete
  coverage.

## Result

| Outcome | Count |
| ------- | ----- |
| Confirmed vulnerabilities | 0 |
| Leads that need a fact of the live account to decide | 2 |
| Coverage units reviewed | 18 of 23 |
| Checks recorded | 33, of which 18 ran in the sandbox |

Both leads are about what a second principal inside the same account could do.
In an account with one owner neither crosses a boundary.

| Lead | What was done |
| ---- | ------------- |
| An approval email that SNS could not deliver was redriven, link included, to the shared dead-letter queue, whose readers nothing restricts | Fixed: the approvals subscription has no dead-letter queue. [ADR 0015](../adr/0015-approval-emails-are-not-redriven-to-a-queue.md) |
| The IAM approval route takes the caller from the event, so a direct invoke of the function can claim to be the approver | Written up as [known limitation 4](../known-limitations.md#4-a-direct-invoke-of-the-approval-function-can-pass-as-the-approver), with a test that fails on purpose and the guardrail to deploy with `approvalAuth=iam` |

## Not reviewed

Left for a `standard` run:

- The notifier trusting its event. In approval mode it takes the approval id
  and the task token from the event and can refresh the stored token of a
  pending record. It is the same family as limitation 4 (a direct invoke of a
  function that trusts its event) and the same guardrail covers it.
- Dependency and build inputs of the CI workflow.
- Quotas and throttling of the link route.
- Accumulation of resources against the time budgets (limitations 2 and 3
  already describe the known cases).
- The wildcard pass over the whole repository.

## What held

Among the invariants that were tested and held: the executor deletes only when
the plan bytes, the detector's hash and a fresh `APPROVED` record all agree; an
execution input reaches nothing but the configuration; one decision wins per
approval; look-alikes of the confirmation code are refused; no Unicode code
point gets a second line or a control character past `one_line`; every DynamoDB
grant is confined by leading key; the executor's delete rights stop at
lab-marked resources; no secret, account id or address is in a tracked file.

The audit also left hardening notes that are not vulnerabilities. They are
listed in the follow-ups file under `docs/superpowers/plans/`.
