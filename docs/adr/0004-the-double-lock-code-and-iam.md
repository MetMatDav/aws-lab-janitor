# 0004. The double lock: code and IAM must both agree

## Status

Accepted on 2026-07-26, kept on 2026-11-02 with a separate executor role.

## Context

A program that deletes cloud resources is one bug away from deleting the wrong
one. A filter in code can be wrong. A policy alone cannot express "only what a
person approved". Either one, alone, is a single point of failure.

## Decision

A resource is deleted only if two independent checks pass. The code selects it:
it carries the lab tag (`Purpose=lab`) or the lab name prefix (`LAB-`), it does
not carry the keep tag (`janitor=keep`), and the executor reads its live state
again immediately before deleting. IAM agrees: the executor role may call the
delete actions only on resources that carry the lab tag (EC2) or whose ARN
starts with the prefix (dashboards and monitors), and an explicit Deny covers
anything carrying the keep tag. The names and values come from one place, so
the code and the policy cannot drift apart.

## Consequences

- A bug that makes the code ask for the wrong resource ends in `AccessDenied`,
  recorded as `FAILED`, not in a deletion.
- The policy cannot express "is in the approved plan" or "is old enough". Those
  two are guaranteed by code only, and the threat model says so.
- The keep tag must match exactly, in code and in IAM alike. A mistyped keep
  tag protects nothing; see [known limitations](../known-limitations.md).
- The policy was checked against the IAM policy engine with 22 simulated calls,
  not only read.

## Where to look

- [src/shared/marking.py](../../src/shared/marking.py) and
  [src/executor/verify.py](../../src/executor/verify.py): the code side.
- [lab_janitor/executor.py](../../lab_janitor/executor.py): the IAM side.
- [docs/evidence/iam-simulation.txt](../evidence/iam-simulation.txt).
- [tests/infra/test_iam_guardrails.py](../../tests/infra/test_iam_guardrails.py):
  `test_executor_mutating_allows_are_locked_to_lab_resources`.
- [tests/property/test_verify_props.py](../../tests/property/test_verify_props.py):
  `test_recheck_never_clears_a_blocked_resource`.
