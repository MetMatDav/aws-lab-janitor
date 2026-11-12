# 0007. Plan, approval, apply

## Status

Accepted on 2026-11-02.

## Context

The first janitor deleted and then sent an email. The owner learned what was
gone after it was gone, and nothing recorded who had decided. The owner asked
for the control a company would expect: know what will happen, decide, and be
able to show later what was decided and by whom.

## Decision

A weekly run is a Step Functions Standard execution in three parts. A read-only
detector writes a plan: a canonical JSON document, stored as a new version in a
versioned bucket and identified by its sha256. A person approves or rejects
that plan, and the execution waits up to 72 hours for the answer. An executor
with its own role applies exactly the version that was approved: it reads that
version, checks its hash against what the detector reported and what the
approval record holds, and walks its items, re-verifying each one. It never
lists resources. Plans are append-only: the bucket policy denies deletion to
everyone but the account root and denies writes to everyone but the detector.

## Consequences

- Nothing is deleted without a recorded approval of a specific document.
- A resource that changed between the plan and the approval is caught by the
  re-verification and skipped, with the reason recorded.
- A run can wait three days. Step Functions Standard charges per state
  transition, not per second, so waiting costs nothing.
- The approval is good for one hour once given ([0013](0013-an-approval-is-good-for-one-hour.md)).
- Rejected: a single Lambda with a "confirm" flag (no waiting, no record), and
  Express workflows (five minutes at most).

## Where to look

- [lab_janitor/workflow.py](../../lab_janitor/workflow.py) and
  [lab_janitor/workflow_approval.py](../../lab_janitor/workflow_approval.py).
- [src/shared/plan.py](../../src/shared/plan.py) and
  [src/shared/plan_store.py](../../src/shared/plan_store.py).
- [src/executor/handler.py](../../src/executor/handler.py): `check_integrity`.
- [tests/unit/test_plan_store.py](../../tests/unit/test_plan_store.py):
  `test_get_plan_hash_reflects_tampered_bytes`.
- [tests/infra/test_audit_store.py](../../tests/infra/test_audit_store.py):
  `test_plans_bucket_policy_denies_delete_except_root`.
- [docs/evidence/controlled-deletion-2026-11-12.md](../evidence/controlled-deletion-2026-11-12.md).
