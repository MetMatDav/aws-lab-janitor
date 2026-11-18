# Known limitations

What this project does not handle, why that is acceptable for what it is (one
owner, one lab account, tens of resources), and what the change would be if
the context grew. Each limitation has a test that fails today on purpose
(`xfail(strict=True)`), so the day one is fixed its test says so.

The first three were found by an adversarial pass over the code on 2026-11-12.
The defects that pass also found, and that were fixed, are in
`docs/superpowers/plans/2026-11-03-plan-1-followups.md`. The fourth was found
by a [security audit](evidence/security-audit-2026-11-18.md) on 2026-11-18.

## 1. The approval id is readable by anyone who can read the audit trail

**What happens.** With `approvalAuth=link` (the default) or `both`, the link in
the approval email is the authority to approve: whoever opens
`/approvals/<approvalId>` and types the four-character code shown on that same
page decides. The id is also:

- the partition key of the approval record (`APPROVAL#<approvalId>`), so it is
  in the audit table and, because CloudTrail data events log item keys, in the
  trail's log bucket;
- part of the Step Functions state (`NewApprovalId` output, the input of the
  reminder execution), so it is in the execution history.

**Who can use it.** Someone with read-only access to the audit table, the trail
bucket or the execution history of this account. They could approve a plan
without receiving the email. They could not change what the plan deletes: the
executor applies only the plan version and hash stored in the record, and
re-checks every resource.

**Why it stands.** In this account those readers are the owner. Hashing the
DynamoDB key alone would not close it, since the id would still sit in the
execution history.

**The mitigation that exists today.** Deploy with `-c approvalAuth=iam -c
approverRoleArn=<role>`. On the IAM route the id decides nothing: API Gateway
authenticates the caller with SigV4 and the handler compares the caller with
the approver role. That holds for a caller who comes through the API; section 4
is what remains.

**What the fix would be.** The secret has to be born where it is used and never
travel through the orchestrator:

1. The notifier, not the state machine, generates a random secret when it
   creates the approval record. The state machine passes only the run id.
2. The record is keyed by `sha256(secret)`. The email link carries the secret;
   the handler hashes the path parameter and looks the record up by the hash.
   The table, CloudTrail and the execution history then hold hashes only.
3. The reminder cannot rebuild the link from a hash. Either it says "use the
   link in the original email", or the secret is stored encrypted with a KMS
   key that only the notifier role may use.
4. The state machine closes the record (`RecordRejected`, `RecordExpired`) by
   run id through a second key or a small Lambda, instead of by approval id.

Test: `tests/adversarial/test_secrets.py::test_no_dynamodb_key_carries_the_approval_id_in_clear`.

## 2. The detector writes its audit items one by one, outside its time budget

**What happens.** The detector has 5 minutes. The sweep stops 120 seconds
before the timeout so the plan and the report can be stored. After that it
writes one audit item per finding with one `PutItem` each and no time check. A
simulated account with 15,000 findings at 10 ms per write overruns the reserve:
the function times out, the run fails, and no report or approval request goes
out. Nothing is deleted and the `ExecutionFailed` alarm fires, so it fails
safe and loudly; but it fails every week until the account is smaller.

**Who can cause it.** Anyone who can create resources the sweep lists.

**Why it stands.** The lab account holds tens of resources. A budget check in
the loop would only choose which audit items to drop.

**What the fix would be.** A different shape for the detection step:

1. A Step Functions Distributed Map over regions (and services), each child
   execution with its own Lambda and its own timeout, instead of one function
   sweeping everything.
2. Findings written as JSON Lines to S3 by each child, then merged into the
   plan by a final step; the plan's hash is still taken over the stored bytes.
3. Audit items written with `BatchWriteItem` (25 per call) by a consumer of
   that file, with unprocessed items retried, instead of inline `PutItem`s.
   The conditional "write once" guarantee moves to the key design (the run id
   in the key already makes the items unique).

Test: `tests/adversarial/test_exhaustion.py::test_audit_writes_of_a_crowded_account_fit_in_the_reserve_the_detector_kept`.

## 3. The executor records every leftover item after its time budget trips

**What happens.** The executor has 10 minutes and stops deleting 30 seconds
before the timeout. From then on each remaining item is still recorded as
`SKIPPED` (`time-budget`) with one DynamoDB write and one EventBridge event. A
simulated plan with 3,000 leftovers at 10 ms per write outlasts the reserve:
the function is killed, `Apply` fails and no result email is sent. Every
deletion made before that is already in the audit table and on the event bus,
so nothing is lost or repeated; the summary is what goes missing.

**Who can cause it.** It needs a plan of thousands of items and an approver who
accepts it.

**Why it stands.** A plan in this account has a handful of candidates, and the
approval email shows the count before anyone approves.

**What the fix would be.**

1. Short term: after the first `time-budget` skip, stop the loop and write one
   record with the number of items left, so the function always returns.
2. At scale: the same Distributed Map as above over chunks of the approved
   plan, each chunk re-verifying and deleting its items within its own
   timeout, with the results aggregated by the map's result writer. A run
   that is interrupted resumes with a new plan the following week rather
   than with a replay: the executor refuses an approval decided more than an
   hour ago.

Test: `tests/adversarial/test_exhaustion.py::test_recording_the_leftovers_fits_in_the_reserve_the_executor_kept`.

## 4. A direct invoke of the approval function can pass as the approver

**What happens.** With `approvalAuth=iam` or `both`, the IAM route reads the
caller from `requestContext.authorizer.iam.userArn` in the event. API Gateway
fills that field in after it authenticates the caller with SigV4. A Lambda
function can also be invoked directly, and whoever does that writes the whole
event, that field included. The handler cannot tell the two apart: nothing in
an event proves where it came from.

**Who can use it.** Someone who holds `lambda:InvokeFunction` on the approval
function, cannot assume the approver role, and knows the id of a pending
approval (limitation 1). They could approve a plan in the approver's name. As
in limitation 1, they could not change what the plan deletes. With
`approvalAuth=link`, the default, the IAM route answers `404` and this does not
apply.

**Why it stands.** In this account the only principal that can invoke the
function is the owner, who is also the approver. The reader limitation 1 is
about, with read-only access, cannot invoke a function. Closing it in code
means a second authentication protocol on a route that has never run live.

**The mitigation that exists today.** Keep `lambda:InvokeFunction` on the
approval function away from every principal but the approver. The runbook says
how to check it and how to deny it:
[Who may invoke the approval function](runbook.md#who-may-invoke-the-approval-function).
API Gateway is not affected: it invokes through the function's resource policy,
as a service.

**What the fix would be.** The handler authenticates the caller itself instead
of trusting the event:

1. `scripts/approve.py` sends, with the decision, a presigned
   `sts:GetCallerIdentity` request whose signature covers a header that names
   the approval id and the decision.
2. The handler accepts that request only if it is addressed to the STS endpoint
   of its own region, sends it, and takes the caller from the answer of STS.
   A hand-written event has no such signature to offer.
3. The proof expires in seconds and is bound to one approval and one decision,
   so replaying it repeats a decision that was already made.

The IAM authorizer of the route stays as the first filter.

Test: `tests/adversarial/test_approval_endpoint.py::test_a_direct_invoke_cannot_pass_as_the_approver`.

## Deliberate choices that can surprise

Not defects: the behaviour is pinned by tests. Listed because both review
passes pointed at them.

- **The keep tag must match exactly.** `janitor=keep` protects a resource;
  `Janitor=keep`, `janitor=Keep` or `janitor=keep ` with a trailing space do
  not. AWS tag keys and values are case-sensitive, and the IAM deny on the
  executor role compares the same exact strings, so the code and the policy
  agree. The cost is that a mistyped keep tag fails open. The approval email
  lists every candidate, which is the last check.
- **An empty `allowedRegions` means every region.** Set it to restrict where
  the executor may delete.
- **`minAgeDays: 0` in an execution input is honoured.** It is how the
  controlled deletion test runs. Dashboards and Internet Monitor monitors have
  no age rule at all: they are selected by name prefix.
