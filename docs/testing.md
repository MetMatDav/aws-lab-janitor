# How it is tested

The janitor deletes resources, so the tests are organized around one question:
what would have to go wrong for it to delete the wrong thing, and what in the
suite would notice. Counts are from 2026-11-18.

## The layers

| Suite | Tests | What it asks | How |
| ----- | ----- | ------------ | --- |
| `tests/unit` | 492 | Does each function do what was meant | The real code against recorded AWS responses (botocore `Stubber`), which also fixes the order of the calls |
| `tests/infra` | 309 | Is the infrastructure what the design says | Assertions on the synthesized template, a snapshot of it, and guardrails that scan every IAM statement of the stack |
| `tests/adversarial` | 538 | What can someone else make it do | Attacks from three footholds: seeing an approval URL, being able to tag resources, being able to start executions or write the plans bucket |
| `tests/property` | 17 | Does this hold for every input | Generated inputs and an invariant (`hypothesis`), including a state machine over the approval record |
| `tests/docs` | about 80 | Do the documents still describe the code | Links and anchors resolve, the README's tables and diagram match the code, no personal data in the tree |

Outside pytest:

- **cdk-nag, strict.** `make nag` synthesizes with the AWS Solutions and
  Serverless rule packs and fails on any finding that is not acknowledged with
  a reason next to the construct it concerns.
- **The IAM policy engine.** `make simulate` sends the executor's policy to
  `iam:SimulateCustomPolicy` with 22 calls that must be allowed or denied: a
  lab-tagged snapshot, an untagged one, a keep-tagged one, a production name,
  an action the role must never have. The result is
  [evidence/iam-simulation.txt](evidence/iam-simulation.txt). It needs
  credentials and changes nothing.
- **cfn-lint** on the synthesized template, in CI.

## Running them

```
make install     # virtualenv and dependencies
make lint        # ruff
make test        # everything under tests/
make nag         # cdk synth with cdk-nag, strict
make simulate    # the IAM simulation; needs AWS credentials
```

`make test`, `make synth` and `make nag` need no AWS credentials: without them
the Makefile supplies a placeholder account and region. The property tests run 100
examples each by default and 300, derandomized, with `HYPOTHESIS_PROFILE=ci`,
which is what CI uses so that a failure can be reproduced.

## Tests that fail on purpose

Four tests are marked `xfail(strict=True)`: they assert the behaviour the code
should have and does not, for the four
[known limitations](known-limitations.md). They keep the suite green today and
turn it red the day one of them is fixed without removing the mark.
`pytest tests/adversarial --runxfail` shows them failing.

## Are the tests any good

A test suite can pass because it checks nothing. Two things were done about
that.

- Every fix of 2026-11-12 started from a test seen failing for the right
  reason before the code changed.
- The property tests were run against code broken on purpose, in memory: an
  executor that ignores the keep tag, one that ignores the lab tag, and an
  approval store that lets an expired record be claimed, a decided record be
  expired, a record be reminded twice or finalized from the wrong state. Each
  mutant made a property fail.

## What ran against the real account

Tests against recorded responses prove the code, not the account. These ran
live, in the owner's account:

| Date | What | Record |
| ---- | ---- | ------ |
| 2026-07-27 to 2026-11-09 | Sixteen weekly runs of the first janitor | [evidence/legacy-weekly-runs.md](evidence/legacy-weekly-runs.md) |
| 2026-11-03 | First dry-run of this stack: 25 regions swept. Its report was published and reached nobody, because the subscription was not confirmed yet; ADR 0012 starts there | [runbook](runbook.md#first-dry-run-what-a-healthy-run-looks-like) |
| 2026-11-11 | Approval through the email link with deletions disabled | [runbook](runbook.md#rehearsal-record-2026-11-11) |
| 2026-11-11 | The reminder; a failed execution raising its alarm and recovering | [runbook](runbook.md#operational-rehearsal-record-2026-11-11-and-2026-11-12), [alarm history](evidence/alarm-history-2026-11-12.json) |
| 2026-11-12 | A wrong confirmation code, then a rejection; CloudTrail data events | [runbook](runbook.md#operational-rehearsal-record-2026-11-11-and-2026-11-12) |
| 2026-11-12 | A false alarm on a delivered email, which changed the design | [ADR 0012](adr/0012-refuse-to-publish-instead-of-alarming-on-delivery.md) |
| 2026-11-12 | A real deletion: three decoys approved and deleted, a keep-tagged one left alone | [evidence/controlled-deletion-2026-11-12.md](evidence/controlled-deletion-2026-11-12.md) |
| 2026-11-14 | An approval nobody answered expiring after 72 hours | [evidence/expiry-2026-11-14.md](evidence/expiry-2026-11-14.md) |
| 2026-11-16 | The first scheduled run with deletions armed: started by the schedule, nothing to delete, report delivered | [runbook](runbook.md#cutover-record-2026-11-12) |
| 2026-11-18 | A security audit of the source in a sandbox, with no access to the account: nothing confirmed, two leads | [evidence/security-audit-2026-11-18.md](evidence/security-audit-2026-11-18.md) |

## What never ran live

Covered by tests against recorded responses only:

- The approval route authenticated with IAM.
- The IAM half of the double lock. The executor's policy was simulated against
  the policy engine, with the tags supplied by the simulation; no live call by
  the executor has been seen denied, because the code has never asked for a
  resource it should not.
- Releasing an Elastic IP and deleting an Internet Monitor monitor.
- A reminder email after a real wait of 60 hours. The reminder was sent once
  from an execution started by hand with a wait of five seconds; the two that
  waited their 60 hours found the approval already decided and sent nothing.
- The dead-letter queue. No start by the schedule and no email delivery is
  known to have failed, so the path from a failure to the queue and its alarm
  was never exercised.
- The notifier refusing to publish to a topic without a confirmed subscriber.
- A decision whose outcome is unknown (`504`) and the manual reset that follows.
- The executor running out of its time budget.
- The minimum age rule on a real resource: the controlled deletion ran with a
  minimum age of zero.
- The quickstart of the README from a fresh account. The author's account was
  deployed with the same commands and its own settings.
