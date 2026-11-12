# 0005. The dead-letter alarm treats missing data as healthy

## Status

Accepted on 2026-07-26.

## Context

The dead-letter queue is empty and untouched for months at a time. SQS stops
publishing CloudWatch metrics for a queue that has been inactive for several
hours, so the metric the alarm watches usually has no datapoints at all. With
CloudWatch's default, the alarm would sit in `INSUFFICIENT_DATA` forever, and
an alarm that is never `OK` trains its reader to ignore it.

## Decision

The alarm on the queue (`ApproximateNumberOfMessagesVisible`, maximum over five
minutes, greater than zero) treats missing data as not breaching. No datapoints
means no messages, and the alarm reads `OK`.

## Consequences

- The alarm has two honest states: `OK`, and `ALARM` when a message arrives. It
  notifies on both transitions.
- If SQS itself stopped reporting while holding messages, the alarm would stay
  `OK`. The heartbeat alarm, which treats missing data the opposite way, covers
  the case where the janitor did not run at all.
- The same reasoning does not apply to the heartbeat: there, no data means no
  run, so missing data is breaching. The two alarms are mirror images on
  purpose.

## Where to look

- [lab_janitor/alarms.py](../../lab_janitor/alarms.py): `_dlq_not_empty` and
  `_heartbeat`.
- [tests/infra/test_alarms.py](../../tests/infra/test_alarms.py): `test_dlq_alarm`
  and `test_heartbeat_alarm_is_seven_daily_periods_breaching`.
