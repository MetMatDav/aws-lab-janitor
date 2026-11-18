# 0012. Refuse to publish to nobody, instead of alarming on delivery

## Status

Accepted on 2026-11-12. Replaces two alarms that lived for one day.

## Context

SNS accepts a publish to a topic whose only subscription is still waiting to be
confirmed, and reports neither a delivery nor a failure. That happened on the
first manual run: the report was published and reached nobody, with no signal.
The first answer was an alarm per topic on `published - delivered - failed`
over one hour. On its first rehearsal it fired on a message that was delivered:
CloudWatch evaluates the alarm every minute over a sliding hour, and it
received the publish datapoint of the message before its delivery datapoint.
The alarm recovered a minute later. No alarm setting asks for a breach to last,
because one message is one datapoint.

## Decision

The check moved from after the fact to before it. Before it reads, writes or
publishes anything, the notifier lists the subscriptions of the topic and
raises an error if none is confirmed. The run fails in that state and the
failure goes to the alerts topic. An approval request checks both topics first,
so a request nobody would receive is never stored. Both alarms were removed.

## Consequences

- The condition is detected when it matters, deterministically, with no metric
  lag and no alarm to tune.
- A run that cannot reach its reader fails at once instead of waiting 72 hours
  for an approval nobody was asked for.
- It checks the subscription, not the delivery. An address that is confirmed
  but bounces is still the dead-letter queue's job, for reports and alerts.
  Approval emails are not sent to the queue
  ([ADR 0015](0015-approval-emails-are-not-redriven-to-a-queue.md)).
- The alerts topic has no such guard: its failure would have nowhere to go.
- The refusal itself has only been exercised in tests. Doing it live means
  unsubscribing the owner's own mailbox.

## Where to look

- [src/notifier/delivery.py](../../src/notifier/delivery.py): `require_subscriber`.
- [docs/evidence/alarm-history-2026-11-12.json](../evidence/alarm-history-2026-11-12.json):
  the false alarm, with the datapoints CloudWatch evaluated.
- [tests/unit/test_notifier_delivery.py](../../tests/unit/test_notifier_delivery.py):
  `test_unconfirmed_subscriptions_do_not_count`.
- [tests/unit/test_notifier_approval.py](../../tests/unit/test_notifier_approval.py):
  `test_request_approval_writes_nothing_when_nobody_would_get_the_request`.
- [tests/infra/test_alarms.py](../../tests/infra/test_alarms.py):
  `test_no_alarm_compares_sns_publishes_with_deliveries`.
