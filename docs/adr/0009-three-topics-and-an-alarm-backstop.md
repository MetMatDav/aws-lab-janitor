# 0009. Three topics and an alarm as a backstop

## Status

Accepted on 2026-11-02.

## Context

The janitor sends three kinds of email with different weight. A weekly report
is information. An approval request is an authorization: whoever receives it
can approve a deletion. An alert means the janitor itself is broken. With one
topic, anyone added to read reports could approve, and an alert would arrive
looking like a report.

## Decision

Three SNS topics: reports (the weekly report and the outcome of each run),
approvals (requests and reminders) and alerts (alarms and failed runs). Only
the notifier function may publish to the first two; the approvals topic also
refuses new subscriptions from anyone but the deployment role and the account
root. A run that fails sends its own alert from a `NotifyFailure` state. An
alarm on failed and timed-out executions stays as a backstop for failures that
never reach that state.

## Consequences

- Who can approve is decided by one subscription, protected by a topic policy.
- A failed run usually produces two alert emails, one from the state and one
  from the alarm. That is accepted: the alarm is what caught the run whose
  input could not be parsed, on 2026-11-11, which failed in its first state.
- Nothing can alert about the alerts topic. Its subscription has to be
  confirmed by hand first, and the runbook says so.
- Each subscription must be confirmed once. The notifier refuses to publish to
  a topic with no confirmed subscription
  ([0012](0012-refuse-to-publish-instead-of-alarming-on-delivery.md)).

## Where to look

- [lab_janitor/notifications.py](../../lab_janitor/notifications.py).
- [lab_janitor/alarms.py](../../lab_janitor/alarms.py): `_execution_failed`.
- [tests/infra/test_notifications.py](../../tests/infra/test_notifications.py):
  `test_three_topics_and_three_subscriptions`,
  `test_approvals_topic_denies_subscribe_except_cfn_exec_role_and_root`,
  `test_approvals_topic_denies_publish_except_notifier_role`.
- [docs/evidence/alarm-history-2026-11-12.json](../evidence/alarm-history-2026-11-12.json).
