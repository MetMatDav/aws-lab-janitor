# 0013. An approval is good for one hour

## Status

Accepted on 2026-11-12, after the adversarial review.

## Context

The executor applied any plan whose approval record read `APPROVED`. The record
kept that status forever. Everything the executor needs as input is readable
inside the account, so someone able to invoke the function directly, or to
redrive an old execution, could apply an approved plan again weeks later. The
live checks would still run, but they answer "is this resource deletable now",
not "did someone approve deleting it now": an item skipped the first time for
being too young would pass them later.

## Decision

The executor refuses an approval that was decided more than one hour ago, or
whose decision time is missing or cannot be compared with the clock. In a
normal run the executor starts seconds after the decision, so the window only
ever closes on a replay.

## Consequences

- An approval authorizes one application of one plan, now.
- A legitimate redrive of a failed apply, more than an hour after the decision,
  is refused as well. The answer is a new run and a new approval; the runbook
  says so.
- The window is measured from the decision, not from the expiry of the request:
  a replay one day after the decision is a replay even if the request would
  still have been open.

## Where to look

- [src/executor/handler.py](../../src/executor/handler.py): `APPLY_WINDOW` and
  `check_integrity`.
- [tests/unit/test_executor_handler.py](../../tests/unit/test_executor_handler.py):
  `test_stale_or_undated_approval_is_refused`.
- [tests/adversarial/test_executor_deletion.py](../../tests/adversarial/test_executor_deletion.py):
  `test_apply_replayed_long_after_the_approval_is_refused`.
