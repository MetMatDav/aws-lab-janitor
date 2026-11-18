# 0001. Two retry layers and a shared dead-letter queue

## Status

Accepted on 2026-07-26 for the first janitor (one Lambda started by a schedule),
carried over to the state machine on 2026-11-02. Amended on 2026-11-18 by
[ADR 0015](0015-approval-emails-are-not-redriven-to-a-queue.md): the approvals
subscription no longer redrives to the queue.

## Context

A weekly job fails in two different places. The schedule may be unable to start
it (a throttled API, a permission removed by mistake). Or it starts and a step
inside it fails. The first janitor inherited two defaults: the schedule would
keep trying to start it for up to 24 hours, and Lambda would run a failed
asynchronous invocation two more times. A sweep that failed halfway could run
three times, and a start that never succeeded told nobody.

## Decision

Retries are bounded at each layer and whatever is given up on lands in one
queue. EventBridge Scheduler tries to start the execution twice more within one
hour, then sends the event to the dead-letter queue. Inside the run, each
Lambda task is retried by Step Functions only for what a retry can fix: Lambda
service errors (2 seconds, doubling, six attempts) and throttling (30 seconds,
doubling, five attempts). An error
raised by the function's own code is not retried; it is caught and sent to the
alerts topic. The same queue receives an email that SNS could not deliver, from
the redrive policy of each of the three subscriptions.

## Consequences

- The schedule gives up on a start within the hour, and the failure is visible
  then instead of the next day. Scheduler delivers at least once, so a second
  start for one tick is possible in principle; each run is independent and
  needs its own approval.
- One queue and one alarm cover three sources. The cost is that the alarm says
  "something is in the queue", not what: the runbook explains how to read the
  message and tell a failed start from a failed email.
- boto3 calls inside the functions keep their own bounded retries (standard
  mode, up to three), so one AWS call can take four tries; the sweep's time
  budget accounts for that.
- Rejected: a dead-letter queue per source. Three queues and three alarms for
  events that should never happen.

## Where to look

- [lab_janitor/workflow.py](../../lab_janitor/workflow.py): the schedule target
  with `retry_attempts=2`, `max_event_age` one hour and the queue.
- [lab_janitor/workflow_approval.py](../../lab_janitor/workflow_approval.py):
  `add_throttle_retry`.
- [lab_janitor/notifications.py](../../lab_janitor/notifications.py): the
  redrive of each subscription.
- [tests/infra/test_alarms.py](../../tests/infra/test_alarms.py): `test_dlq_alarm`.
