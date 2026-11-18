# 0015. Approval emails are not redriven to a queue

## Status

Accepted on 2026-11-18, after a security audit. Amends
[ADR 0001](0001-two-retry-layers-and-a-shared-dead-letter-queue.md), which sent
the failed emails of all three subscriptions to the shared dead-letter queue.

## Context

An approval email carries a link that decides a plan. The approvals topic is
closed for that reason: only the notifier publishes to it, and only a stack
deployment or the account root can add a subscriber. Its email subscription
also had a redrive policy, like the other two. If SNS gave up on delivering an
approval email, the whole message, link included, went to the shared queue and
stayed there for 14 days. The queue policy says who may send to it and nothing
about who may read it, and its encryption is SQS-managed, so no key policy
narrows that either. The readers were whoever holds `sqs:ReceiveMessage` in the
account. The lock on the topic had a second door.

Two independent reviewers of the audit reported it. Nothing was confirmed: it
needs a failed delivery and a second principal in the account, and this account
has one owner.

## Decision

The approvals subscription has no dead-letter queue. An approval email that SNS
cannot deliver is dropped, as SNS does without a redrive policy. The report and
alert subscriptions keep theirs: their messages carry no authority.

## Consequences

- A working approval link is never stored in a queue. The topic's lock is the
  only way to the message again.
- A failed approval delivery no longer raises `DlqNotEmpty`. It is still not
  silent for long, and it fails safe: the reminder is a second attempt, the
  request expires after 72 hours with nothing deleted, and the expiry notice
  goes out on the reports topic, whose failures do reach the queue. SNS also
  counts the failure in `NumberOfNotificationsFailed` of the approvals topic.
- The notifier still refuses to publish an approval request when the topic has
  no confirmed subscriber
  ([ADR 0012](0012-refuse-to-publish-instead-of-alarming-on-delivery.md)), which
  is the failure seen in practice.
- Rejected: a second queue for approvals with a policy that denies reading to
  everyone but the account root. It keeps the alarm, and it keeps a bearer link
  at rest for 14 days, for an event that has never happened.
- Rejected: an alarm on `NumberOfNotificationsFailed`. One more alarm for the
  same event; the expiry notice already tells the owner.

## Where to look

- [lab_janitor/notifications.py](../../lab_janitor/notifications.py): the
  `ApprovalsEmail` subscription, without `dead_letter_queue`.
- [tests/infra/test_notifications.py](../../tests/infra/test_notifications.py):
  `test_approval_emails_are_never_parked_in_a_queue`.
- [docs/evidence/security-audit-2026-11-18.md](../evidence/security-audit-2026-11-18.md):
  the audit that found it.
