# 0011. The reminder is an email from a second state machine

## Status

Accepted on 2026-11-11. Replaces the "approval pending too long" alarm of the
design.

## Context

An approval request waits 72 hours and then expires. The design asked for a
custom metric, `ApprovalPending`, and an alarm when it passed 60 hours. A
metric needs something that publishes it while the run waits, and a waiting
Step Functions task publishes nothing. An alarm would also reach the alerts
mailbox with no link, when what is wanted is for the approver to decide.

## Decision

When the weekly run asks for approval it also starts a second, small state
machine: wait 60 hours, then invoke the notifier in reminder mode. The notifier
stamps the approval record with a conditional update that succeeds only while
the approval is still pending, unexpired and not yet reminded, and only then
sends the same link to the approvals topic. Starting the reminder is best
effort: if it cannot be started, the run continues and asks for approval
anyway.

## Consequences

- The reminder reaches the person who can act, with the means to act.
- It is sent at most once, and never for an approval that was decided: the
  condition is in the write, not in a check before it.
- No new function and no permission to pass roles: the weekly machine may start
  exactly one state machine.
- A failed reminder raises its own alarm. A reminder that could not be started
  leaves only a field in the execution history.
- The approval id travels in the reminder's input, one more place where it can
  be read.

## Where to look

- [lab_janitor/reminder.py](../../lab_janitor/reminder.py) and `StartReminder` in
  [lab_janitor/workflow_approval.py](../../lab_janitor/workflow_approval.py).
- [src/notifier/reminder.py](../../src/notifier/reminder.py) and `mark_reminded`
  in [src/shared/approvals.py](../../src/shared/approvals.py).
- [tests/infra/test_reminder.py](../../tests/infra/test_reminder.py):
  `test_weekly_role_can_start_only_the_reminder`.
- [tests/unit/test_notifier_reminder.py](../../tests/unit/test_notifier_reminder.py):
  `test_reminder_is_silent_when_the_approval_is_not_pending`.
