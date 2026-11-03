# Plan 1 follow-ups

Items deferred from the Plan 1 reviews (task reviews and the whole-branch review), triaged by where they belong. Nothing here blocks the dry-run deployment.

## Spec corrections to make before Plan 2 starts

- **EIP two-run rule cannot fire.** The handler writes `PROPOSED` only for candidates, and an EIP becomes a candidate only if a previous live `PROPOSED` exists, so no EIP is ever proposed. Fix: write a distinct event (for example `ELIGIBLE`) for idle, lab-tagged, unkept EIPs and require it from the immediately previous live run; update spec sections 4.2, 4.5 and 4.8. Today this fails safe (EIPs are never candidates).
- **Audit sort key includes the kind:** `RESOURCE#<region>#<kind>#<resourceId>#<EVENT>`. Spec 4.5 still shows the old key.
- **Run id format and candidate key:** `YYYYMMDDTHHMMSSZ-<4 hex>` and `resourceId` (plan wins over the spec 4.4 example).
- `put_run_summary` hardcodes `actor = "lambda:detector"`; the executor needs an `actor` parameter.
- `allow_plan_writes` must be called exactly once (a second call would deny every writer); the executor must not call it.
- The stack outputs carry the `Workflow` construct prefix; declare them at stack level or set export names.

## Plan 2 (approval flow and executor)

- Exclude the janitor's own resources (tag `Project=aws-lab-janitor`) from the inventory.
- Pass the time budget to the remaining paginated listings (NAT gateways, VPC endpoints, images, RDS, ELB, global, monitor) or lower sweep retries; add a mid-loop deadline test.
- Route the four tag-lookup variants through `read_tags` in `common.py`; use `.get("ARN")` for ElastiCache clusters.
- Tests still missing: prefix shorter than 3, empty markers, lowercase tag shape, `is_kept` negatives, region fallback, `to_dict` shapes, `enabled_regions`, `main()` never writes, subject boundary 99/100, config parsing matrix, delete-Deny `Resource` pinned, managed policies and `NotAction` in the IAM guardrail helpers (extend to the executor role).
- Use `Stack.partition` instead of a hardcoded `arn:aws` in the plans bucket policy.
- `NotifyFailure` uses `States.Format` with `$.error.Cause`; a caught error without `Cause` would raise `States.Runtime` (the `ExecutionFailed` alarm is the backstop).
- "AMI-backed" reason only set for lab snapshots; the unreachable `attached` reason; `global` as a service label; audit warnings without `runId`.
- Request a Lambda concurrency quota increase (to at least 101) and redeploy without `-c reservedConcurrency=false`.

## Plan 3 (operations, CloudTrail, cutover)

- `createTrail=dataEventsOnly`: an organization Control Tower trail already delivers management events for this account.
- Before the controlled deletion test, switch the old `AwsJanitorStack` to dry-run.
- Delivery evidence, not just publish evidence: the first manual run (2026-11-03) published to a reports topic whose email subscription was still `PendingConfirmation`, so the report vanished with no failure metric and no alarm. Add a `NumberOfNotificationsDelivered` check to the runbook's healthy-run shape and consider an alarm on `Published - Delivered > 0` for the reports topic (the alerts topic cannot alarm on itself; document that gap).

## Plan 4 (story and publication)

- Reword the cdk-nag reasons that are only partly true (shared DLQ also receives SNS redrive; the Python 3.13 "roadmap" entry; `AwsSolutions-S1` depends on `createTrail`).
- Owner decision: commit author identity (the current history carries the owner's personal address); switch to a GitHub noreply address before publishing if desired.
- Update the spec with the corrections above; update the plan text that lacks `--` in the cfn-lint command.
- Infra test fixtures (`stack`/`template`) synthesize without the `cdk.json` feature flags; align them with the CLI synth so the snapshot matches a real `cdk synth`.
