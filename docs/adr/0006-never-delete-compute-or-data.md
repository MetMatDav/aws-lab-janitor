# 0006. Never delete compute or data

## Status

Accepted on 2026-07-26, restated as an anti-goal of the design on 2026-11-02.

## Context

The resources that cost the most when forgotten (instances, databases, caches,
load balancers) are also the ones whose deletion cannot be undone. A snapshot
or an idle volume can be recreated from what a lab produces again; a database
cannot. The janitor runs unattended in an account the owner still uses.

## Decision

The janitor reports compute and data stores and never deletes them. It can
delete five kinds of leftovers: EBS snapshots, unattached EBS volumes,
unassociated Elastic IPs, CloudWatch dashboards and Internet Monitor monitors.
This is not a setting. No role in the stack holds a delete, terminate or stop
action for anything else, so no input, plan or approval can cause one.

## Consequences

- The worst case of a compromised approval is the loss of lab snapshots,
  volumes, addresses, dashboards and monitors that someone marked as lab.
- The report still shows the cost of everything else, so the owner deletes the
  expensive things by hand, knowing what they are.
- Adding a deletable kind is a deliberate change in three places (the executor's
  kinds, its policy, the guardrail test), and the test fails if only one moves.

## Where to look

- [src/executor/handler.py](../../src/executor/handler.py): `DELETABLE_KINDS`.
- [lab_janitor/executor.py](../../lab_janitor/executor.py): the only mutating
  statements of the stack.
- [tests/infra/test_iam_guardrails.py](../../tests/infra/test_iam_guardrails.py):
  `test_executor_mutating_allows_are_locked_to_lab_resources` and
  `test_delete_allows_belong_to_the_executor_or_are_log_delivery`.
- [tests/docs/test_readme.py](../../tests/docs/test_readme.py):
  `test_never_touches_is_every_kind_that_cannot_be_deleted`.
