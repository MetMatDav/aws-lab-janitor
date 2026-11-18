# Architecture decision records

One decision per file: what the problem was, what was decided, what it costs,
and which code and which test to open. The first six come from the first
janitor, a single Lambda written in July 2026, and are written about the code
as it is now. The rest were taken for the release that added approval.

| Number | Decision |
| ------ | -------- |
| 0001 | [Two retry layers and a shared dead-letter queue](0001-two-retry-layers-and-a-shared-dead-letter-queue.md) |
| 0002 | [One boto3 session, cached clients and 512 MB](0002-one-boto3-session-and-512-mb.md) |
| 0003 | [Global and regional resources are swept separately](0003-global-and-regional-resources.md) |
| 0004 | [The double lock: code and IAM must both agree](0004-the-double-lock-code-and-iam.md) |
| 0005 | [The dead-letter alarm treats missing data as healthy](0005-dead-letter-alarm-treats-missing-data-as-healthy.md) |
| 0006 | [Never delete compute or data](0006-never-delete-compute-or-data.md) |
| 0007 | [Plan, approval, apply](0007-plan-approval-apply.md) |
| 0008 | [Approval by a link in an email](0008-approval-by-email-link.md) |
| 0009 | [Three topics and an alarm as a backstop](0009-three-topics-and-an-alarm-backstop.md) |
| 0010 | [Audit table write rules and what CloudTrail records](0010-audit-table-write-rules-and-cloudtrail-scope.md) |
| 0011 | [The reminder is an email from a second state machine](0011-the-reminder-is-an-email-from-a-second-state-machine.md) |
| 0012 | [Refuse to publish to nobody, instead of alarming on delivery](0012-refuse-to-publish-instead-of-alarming-on-delivery.md) |
| 0013 | [An approval is good for one hour](0013-an-approval-is-good-for-one-hour.md) |
| 0014 | [Three kinds of tests: examples, adversaries and properties](0014-three-kinds-of-tests.md) |
| 0015 | [Approval emails are not redriven to a queue](0015-approval-emails-are-not-redriven-to-a-queue.md) |
