# 0010. Audit table write rules and what CloudTrail records

## Status

Accepted on 2026-11-02; the trail was added on 2026-11-11.

## Context

The audit table answers, for every resource the janitor touched: who decided,
when, why and what happened. A record that the janitor itself can rewrite is a
log, not evidence. And a record kept only by the system it describes proves
little: someone outside it has to keep one too.

## Decision

Every event of a run is written with a conditional put that fails if the item
exists, so the janitor never overwrites its own history, and no role may delete
items. That condition is in each request, not in IAM, which cannot require it:
a role allowed to put items under `RUN#` could replace one if its code were
changed, and CloudTrail would record the call. Run events
live under `RUN#<runId>` and approval records under `APPROVAL#<id>`, and each
role's grant is confined to one of the two with `dynamodb:LeadingKeys`. The
only updates are the status changes of an approval record, each conditional on
the status it replaces. Outside the table, a CloudTrail trail records data
events for the plans prefix of the bucket and for the table, so every read and
write of either is logged by AWS, not by the janitor. `createTrail` selects a
full trail, data events only (for accounts that already have a trail for
management events) or none.

## Consequences

- The record is tamper-evident, not tamper-proof. An account administrator can
  still change the table; CloudTrail would show that they did.
- CloudTrail logs the key of each item and the expressions used, never a value.
  The approval id is part of a key, so it appears there in clear:
  [known limitations](../known-limitations.md), section 1.
- The deletions themselves are management events. They are in the account's
  event history for 90 days with no trail at all, under the executor's name.
- Data events are billed per event. A weekly run produces a few dozen.
- Approval records are removed by DynamoDB 30 days after they expire. The
  mirror item under the run, with the outcome and the approver, stays.
- With `createTrail=none` there are no data events, and the table is the only
  record of who read or wrote it.

## Where to look

- [lab_janitor/audit.py](../../lab_janitor/audit.py) and
  [src/shared/audit.py](../../src/shared/audit.py).
- [lab_janitor/trail.py](../../lab_janitor/trail.py).
- [tests/infra/test_audit_store.py](../../tests/infra/test_audit_store.py):
  `test_every_dynamodb_write_grant_in_the_stack_carries_leading_keys`.
- [tests/infra/test_trail.py](../../tests/infra/test_trail.py):
  `test_data_selectors_pin_the_plans_prefix_and_the_audit_table`.
- [tests/property/test_approval_state_machine.py](../../tests/property/test_approval_state_machine.py):
  `ApprovalStoreMachine`.
