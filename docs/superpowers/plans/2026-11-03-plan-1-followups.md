# Plan 1 follow-ups

Items deferred from the Plan 1 reviews (task reviews and the whole-branch review), triaged by where they belong. Plan 2 folded in the items under "Done in Plan 2"; the rest is still open. Nothing here blocks the dry-run deployment.

## Done in Plan 2

- `put_run_summary` takes an `actor`; the executor and the state machine write their own events with their own actors.
- `allow_plan_writes` is called exactly once (in the detector construct); the executor does not call it.
- Stack outputs are declared at stack level: `StateMachineArn`, `PlansBucketName`, `AuditTableName`, `ApprovalApiUrl`.
- `Stack.partition` replaces the hardcoded `arn:aws` in the plans bucket policy (and in the new policies).
- The janitor's own resources (tag `Project=aws-lab-janitor`) are excluded from the inventory.
- `NotifyFailure` no longer raises `States.Runtime` when a caught error has no `Cause`.
- The EIP two-run rule works: idle, lab-tagged, unkept EIPs get an `ELIGIBLE` event and are proposed once a live ELIGIBLE record is old enough.
- The audit sort key carries the kind: `RESOURCE#<region>#<kind>#<resourceId>#<EVENT>`.
- Run id format and candidate key: spec v3 (done before Plan 2 started).

## Done in Plan 3

Plan 3 closed on 2026-11-12 with the cutover, the controlled deletion test and
the retirement of the old stack.

- The keep-tag Deny is honored by `cloudwatch:DeleteDashboards` and
  `internetmonitor:DeleteMonitor`: both resource types support
  `aws:ResourceTag`, and `iam:SimulateCustomPolicy` agrees (22 cases,
  `docs/evidence/iam-simulation.txt`).
- `allowedRegions` without `us-east-1` is rejected at synth.
- Every paginated listing of the detector takes the time budget; ElastiCache
  clusters without an ARN no longer drop the region's listing.
- The tests listed as missing after Plan 2 were added, and the suite gained an
  adversarial pass and property tests (see the section of 2026-11-12 below).
- CloudTrail: the stack's trail runs in `dataEventsOnly`, next to the
  organization trail that already delivers management events.
- Alarms: heartbeat, failed execution, failed reminder, dead-letter queue. The
  "published but not delivered" alarm was built, raised a false alarm on the
  first rehearsal and was replaced by a subscription check in the notifier.
- Approval reminder by email after 60 hours.
- Seen working live: a wrong confirmation code, a rejection, the reminder, the
  failure alarm, the data events, and a real deletion after approval.
- The Lambda concurrency quota of the account is 1000 since 2026-11-12.
- The old `AwsJanitorStack` went to report-only and was destroyed on
  2026-11-12. Its log group
  (`/aws/lambda/AwsJanitorStack-JanitorFn320C9ED7-...`, 25 KB, two years of
  retention) was retained by the stack and still exists: it is the only record
  of the weekly runs of the old janitor.

## Still open after Plan 3

- Redeploy without `-c reservedConcurrency=false`, now that the quota allows
  the detector and the executor their reservation of 1.
- The expiry path live: the approval of run `20261111T203848Z-138c` expires on
  2026-11-14 20:09 UTC.
- The IAM approval route live (it needs `approvalAuth=both` and an approver
  role in the deploy command).
- The Elastic IP and Internet Monitor deleters have run against stubs only.
- Decide what to do with the old stack's log group: read what Plan 4 needs
  from it (run counts and durations for the README), then delete it.
- The deployed commit is not shown in the report; a stale deployment is caught
  by the post-deploy check, not by an alarm.
- Cosmetic, no behavior a user sees: the four tag-lookup variants of the
  detector do not share `read_tags`; the "AMI-backed" reason is only set for
  lab snapshots; the unreachable `attached` reason; `global` as a service
  label; audit warnings without `runId`; clients built per request in the
  approval handler and twice in the notifier; `_error_code` duplicated in the
  executor; `APPROVAL_KEYS` versus the inline `RUN#*` condition; monitor action
  literals repeated; the policy scan does not cover KMS key policies or
  `Lambda::Permission` (none present).

## Done in Plan 4

Plan 4 closed on 2026-11-18 with a first push to a private repository.

- The cdk-nag reasons that said more than was true were reworded.
- Commit author identity: the owner decided to keep it as it is.
- The plan text lacking `--` in the cfn-lint command was fixed.
- The spec has a last section, "As built", with what changed while building.
- README, fourteen decision records, a threat model, testing notes and
  evidence, held to the code by `tests/docs`.
- The history was rewritten once, before the first push, to take out the
  names of the owner's real resources, a local path and a profile name.
- The expiry of an approval and the first scheduled run with deletions armed
  were seen live (2026-11-14 and 2026-11-16).
- A final review checked about two hundred claims of the documents against
  the code; what it found was corrected before the push.

## Still open after Plan 4

- The owner makes the repository public and tags `v1.0.0`. The two Mermaid
  diagrams of the README were not seen rendered by the author of these notes.
- Exercising the IAM approval route live; decoys for an Elastic IP and an
  Internet Monitor monitor; seeing the IAM denies on a live call.
- Infrastructure test fixtures synthesize without the `cdk.json` feature
  flags, so the snapshot is not byte-for-byte a real `cdk synth`.
- A CI matrix over the three `createTrail` modes; the history scan is not run
  in CI.
- Transitive Python dependencies are not pinned. `aws-cdk-lib` and the
  standalone `aws-cdk-cloud-assembly-schema` both install
  `aws_cdk/cloud_assembly_schema`; which copy wins depends on install order.
  One test depended on it and no longer does.
- `tests/docs` guards with known holes: a forbidden name glued to other
  characters or split across lines, an account id written with hyphens, links
  in HTML, anchors of headings with emphasis.
- The spec's "As built" section is not a line-by-line comparison.
- The three known limitations (`docs/known-limitations.md`).

## Plan 2 residuals (from the whole-branch review of 2026-11-03)

Deferred to Plan 3 unless noted. None blocks the dress rehearsal.

- Approval handler: a `BotoCoreError` from `finalize` after a delivered callback still returns the generic 500 page (only `ClientError` is caught); the 500 page text ("not recorded, try again") is misleading after an unknown callback outcome, and the runbook status table has no 500 row.
- Approval handler: a record left `DECIDING` (callback outcome unknown or finalize failed) can only be retried after a manual reset of `status` to `PENDING` from an admin session; document the procedure or let the handler treat an old `DECIDING` record as claimable. When the claim fails because the state machine already set `EXPIRED`, the handler answers 409 instead of 410.
- Approval handler: clients are built per request; no IAM-route tests for `EXPIRED` and `TaskTimedOut`.
- Notifier: config validation runs after `approvals.create`; on a retry the email may state a later expiry than the stored record; no request-level test for `refresh_token` raising `AlreadyDecidedError`; the result message has no size cap; `build_dependencies` is imported lazily and clients are built twice; `_one_line` is duplicated; subject and header say "1 resources".
- Executor: the reason for a missing timestamp reads "below minimum age (None days)"; `_error_code` is duplicated; a dashboard item with `arn` `None` would raise `ParamValidationError` (fails safe); the EIP age is deliberately not re-queried at apply time (nothing in the audit table can change), say so in the docstring.
- CDK: config regexes without `re.ASCII`/`fullmatch`; `APPROVAL_KEYS` constant versus the inline `RUN#*` condition; the HTTP API has the generic name `Api`; monitor action literals are repeated in `MUTATING_ACTIONS`; the policy scan does not cover KMS key policies, `Lambda::Permission` or `NotAction` statements (none present); `workflow_approval.py` types `parameters: dict` without arguments and its tests slice join parts by position.
- CLI: the signing region is not derived from the API host (a wrong region shows up as 403); no tests for the `HTTPError`/`URLError` paths; `execute-api:Invoke` for an administrator permission set is unverified until the IAM route is exercised live.
- Spec (Plan 4): the notifier holds `dynamodb:UpdateItem` on `APPROVAL#*` as well as `PutItem` (token refresh on retry); `nag.py` cites ADR-0008 before `docs/adr/` exists.

## Dress rehearsal of 2026-11-11 (Plan 2, Task 10)

The rehearsal passed: run `20261111T192012Z-ecb0` went through approval by email
link and ended with the decoy `SKIPPED` (`deletions-disabled`). Record in the
runbook. What it left open, for Plan 3:

- Live-unverified paths: a wrong confirmation code on a `PENDING` approval
  (expected `400`, record stays `PENDING`), the reject path (`RecordRejected`,
  `MirrorRejected`, `NotifyRejected`), the expiry path and the IAM route with
  `scripts/approve.py`. Fold a reject rehearsal and the wrong-code check into
  the controlled deletion test.
- The first Plan 2 deploy (2026-11-03) rolled back on a placeholder email; the
  stack stayed on the Plan 1 template for eight days with nothing signalling
  it. The heartbeat work in Plan 3 should also make a stale deployment visible
  (for example the deployed commit in the report footer).
- Deviation from the plan text: the agent created and deleted the decoy with
  the administrator profile instead of the owner. Both calls are plain
  CloudWatch dashboard calls on an empty `LAB-` dashboard.
- A decided approval answers `409` to `GET` as well as `POST`; the runbook only
  mentioned the second decision.

## Plan 3 residuals (from the whole-branch review of 2026-11-11)

Fixed in the review's fix pass: finalize is retried three times after a
delivered decision; the runbook passages on `DECIDING`, on repeating `-c`
flags, on the undelivered alarm recovering by itself and on what the data
events contain. Deferred:

- Undelivered alarms: removed on 2026-11-12, see the rehearsal section below.
- Approval handler: if botocore retries the claim `UpdateItem` after a lost
  response, the retry fails against the handler's own `DECIDING` and the
  approver gets `409` with no callback sent; the run waits until it expires.
  Comparing the old item's `approvedBy` with the request would recognize the
  handler's own claim. Rare, fails safe.
- `StartReminder` has no `Retry` before its catch-all: a throttled start drops
  the reminder with no signal but `$.reminderError` in the execution history.
- `ReminderFailed` counts failed reminder executions, not timed-out ones.
- A reminder whose publish fails after `remindedAt` was stamped is never sent
  again (the `ReminderFailed` alarm fires); no test pins that order.
- CI synthesizes only `createTrail=full`; a matrix over the three modes would
  keep `dataEventsOnly` and `none` passing cdk-nag.
- Reminder timing: with the Monday 09:00 schedule the 60 hour reminder lands on
  Wednesday 21:00 Bogota and the record expires Thursday 08:30. 48 hours would
  leave a working day.
- "below minimum age (1 days)" and "1 days old" keep a plural for one day.
- Cosmetic: `stack.trail.trail`; `GLOBAL_SERVICES_REGION` in `config.py`
  repeats `verify.GLOBAL_CLIENT_REGION`.
- Plan 4 threat model: approval ids appear in clear in the CloudTrail data
  events (item keys) and in the reminder execution's input; the committed plan
  files name a local path and an SSO profile.

## Operational rehearsal of 2026-11-11 and 2026-11-12 (Plan 3, Task 8)

Record in the runbook. Open after it:

- Fixed on 2026-11-12: `ReportsUndelivered` raised a false alarm on a
  delivered message (`ALARM` 17:42:00 UTC, `OK` 17:43:00). The rejection email
  was published at 17:41:28; the alarm, evaluated every minute over a sliding
  hour, saw `published` 2 and `delivered` 1 for one evaluation. No alarm
  setting asks for a breach to last, because one message is one datapoint.
  Both undelivered alarms are gone. The notifier now lists the subscriptions of
  a topic before publishing (`notifier/delivery.py`) and fails the run when
  none is confirmed, before it reads, writes or sends anything. This replaces
  decision 3 of the Plan 3 document. Not yet deployed; the refusal itself is
  covered by unit tests only, and the first run after the deploy shows whether
  the new `sns:ListSubscriptionsByTopic` grant works.
- Left as they are: the alerts topic has no guard (its failure would have
  nowhere to go); a subscription that is confirmed but bounces is the
  dead-letter queue's alarm; a result email refused after the deletions leaves
  the outcome in the audit table only.
- Plan 4: the spec and the Plan 3 document still describe the two alarms.
- The approval of run `20261111T203848Z-138c` is `PENDING` and its execution
  waits in `RequestApproval`. It expires on 2026-11-14 20:09 UTC; check then
  for `RecordExpired`, `MirrorExpired`, `NotifyExpired`, `Expired`, the
  `EXPIRED` record and the expiry email, and add the result to the runbook.
  Its decoy is gone, so nothing depends on it.
- The second submission of the form was a page reload (`GET 409`), not a
  `POST`. The `POST 409` was seen on 2026-11-11.
- `ExecutionFailed` stayed in `ALARM` for 15 minutes on a 5 minute period. Not
  a problem; the runbook should not promise a faster recovery.
- Still unit tests only: the IAM approval route.

## Review passes of 2026-11-12 (property review and adversarial suite)

Two agents reviewed the code before the cutover. One read the tests against
property-based testing (no test was property-based; three counterexamples
found by random inputs). The other attacked the code from three footholds and
left `tests/adversarial/` (485 passing tests that pin defences, strict xfail
for what broke). No way to delete something outside an approved, re-verified
plan was found.

Fixed the same day:

- Executor: an `APPROVED` record decided more than an hour ago is refused
  (replay by redrive or direct invoke); an AMI lookup that reports more pages
  and a dashboard whose ARN does not name the dashboard being deleted are
  skipped.
- Config: flags accept only `true` or `false` (a mistyped `janitorEnabled`
  left the schedule on); `minAgeDays` capped at 3650 in the context and in the
  execution input (a huge value crashed the detector); integers must be ASCII
  digits; the approver role ARN and the emails accept only the shapes IAM and
  SNS accept.
- Emails: ids, kinds, regions, reasons and the approver line go through
  `shared.text.one_line`, which collapses every Unicode line break and drops
  control and format characters (a User-Agent could forge lines in the outcome
  email); the weekly subject says "1 candidate".
- Approval endpoint: any failure after the claim other than a refused callback
  answers `504`; the catch-all logs an AWS error code, never its message; the
  confirmation code is compared as ASCII.
- IAM: the detector's `dynamodb:Query` grant names the index only.
- Plan: findings that share an identity are ordered by their whole content, so
  the same findings always hash to the same plan.
- Property tests with `hypothesis` under `tests/property/`.

Not fixed, by decision of the owner, and written up with the fix each would
need in `docs/known-limitations.md`: the approval id readable in the audit
table, CloudTrail and the execution history; the detector's and the executor's
audit writes outside their time budget with thousands of resources.

Left as they are, pinned by tests: the keep tag matches exactly; an empty
`allowedRegions` means every region.

Not covered by either pass: live AWS behaviour (EC2 tag character set, what
SNS does with control characters, API Gateway header decoding), Step Functions
semantics (`States.JsonMerge`, redrive), `scripts/approve.py` beyond its host
pattern (it signs a request to whatever https host `--url` names), the other
detectors, the reminder flow.

## Cutover and controlled deletion test of 2026-11-12 (Plan 3, Tasks 10 and 11)

Done; records in the runbook and in `docs/evidence/`. Open after them:

- Destroy the old stack and its retained log group (Task 12).
- The approval of run `20261111T203848Z-138c` still expires on 2026-11-14
  20:09 UTC: verify the expiry path then. If it were approved by mistake
  before that, the executor would find its only candidate gone.
- The controlled test had no Elastic IP or Internet Monitor decoy; those two
  deleters have run against stubs only.
- The owner can add redacted screenshots of the approval and result emails to
  `docs/evidence/`.
- The Lambda concurrency quota request (Task 9) is still to be made; until
  then deploys carry `-c reservedConcurrency=false`.

## Security audit of 2026-11-18

Record in `docs/evidence/security-audit-2026-11-18.md`. Done after it:

- The approvals subscription no longer redrives to the dead-letter queue
  (ADR 0015).
- The direct invoke of the approval function is known limitation 4, with its
  strict `xfail` test and the guardrail in the runbook.

Open after it, none of them a vulnerability:

- A `standard` run of the audit. First unit: the notifier trusts its event
  (`approvalId` and `taskToken` in approval mode, `refresh_token` on a pending
  record), the same family as limitation 4.
- `scripts/approve.py` follows redirects and keeps the signed headers and the
  session token on the redirected request. The signature is bound to the first
  host, so nothing is gained; refuse any 3xx.
- `src/approval/decision.py`: a lost answer to the callback, retried by boto3,
  can come back as `TaskTimedOut` and mark the record `EXPIRED` while the run
  resumed. It fails safe (the executor wants `APPROVED`). Use one attempt for
  the callback client so a lost answer takes the `504` path.
- The executor puts the approval id in its EventBridge events. It is decided by
  then; a short hash would do.
- CI: no top-level `permissions` block, and actions pinned by tag, not by
  commit.
- The Deny on `janitor=keep` relies on `aws:ResourceTag/janitor` for dashboards
  and monitors; confirm in the Service Authorization Reference that both
  services send it.
- An invalid `minAgeDays` in an execution input falls back silently to the
  deploy-time value.
- `scheduleTimezone` is not validated at synth like the other settings.
- `.gitignore` does not list `.env`, `*.pem` or `*.key`. None is tracked.
- The controlled deletion evidence publishes a decided approval id and the ids
  of the deleted decoys. Neither is usable; decide before making the
  repository public whether to keep them.
