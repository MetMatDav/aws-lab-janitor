# Lab Janitor Plan 3: Operations, CloudTrail and Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the janitor operable and provable: every silent failure gets an alarm, a pending approval gets a reminder, CloudTrail records the data events of the audit trail, the IAM claims are simulated and committed as evidence, and the live stack takes over from the old one after a supervised deletion of decoy resources.

**Architecture:** A new `Alarms` construct owns every CloudWatch alarm (the two existing ones move there; heartbeat, reminder failure and undelivered-notification alarms are new). A second, three-state state machine (`Wait` 60 h, notifier in `reminder` mode, `Succeed`) is started fire-and-forget by the weekly workflow right before `RequestApproval`; the notifier marks the approval record `remindedAt` with a conditional update and emails the approvals topic only if the approval is still `PENDING`. A new `AuditTrail` construct creates a CloudTrail trail with advanced event selectors (data events of the plans prefix and the audit table; management events only in `full` mode) and its log bucket, selected by the `createTrail` context value. `tests/iam/simulate_policies.py` runs `iam:SimulateCustomPolicy` against the synthesized executor policy. The remaining tasks are live: deploy, rehearse the reject and failure paths, switch the old stack to dry-run, enable deletions, run the controlled deletion test and retire the old stack.

**Tech Stack:** Python 3.13, boto3 from the Lambda runtime, AWS CDK v2 (aws-cdk-lib 2.275.0; adds `aws_cloudtrail` L1 `CfnTrail`), pytest + botocore Stubber, ruff, cdk-nag 3.0.2, cfn-lint.

**Spec:** `docs/superpowers/specs/2026-11-02-lab-janitor-portfolio-design.md` v3. Sections implemented here: 4.5 (external evidence: CloudTrail, `createTrail`), 4.10 (heartbeat and the approval reminder), 5.3 (`createTrail`), 5.4 (IAM simulation), 5.5 (runbook), 6 (controlled deletion test), 7 (phases 4 and 5). Left for Plan 4: README, ADRs, threat model, evidence gallery, publication.

**Follow-ups folded in** (from `docs/superpowers/plans/2026-11-03-plan-1-followups.md`): the keep-tag Deny on dashboards and monitors (documented support plus simulation); `allowedRegions` must include `us-east-1`; delivery evidence for the reports and approvals topics; the approval handler, notifier, executor, CDK and CLI residuals listed in Task 6; the time budget on the remaining listings and the missing tests listed in Task 7; the reject, wrong-code and reminder paths exercised live in Task 8. What stays open is listed under "Left open" at the end.

## Decisions that depart from the spec

Recorded here so the owner can overrule them before the deploy in Task 8.

1. **Approval reminder is an email, not a metric alarm** (spec 4.10, row "Approval pending too long"). A custom `ApprovalPending` metric needs something that emits it periodically while the run waits, and nothing does. A reminder state machine that waits 60 h and asks the notifier to email the approvals topic (with the same link) reaches the approver directly, adds no Lambda and needs no `iam:PassRole` (which the stack-wide guardrail test forbids). Cost if wrong: one small state machine to remove.
2. **`tests/iam/simulate_policies.py`, not `.sh`** (spec 5.4). The script parses the synthesized template and calls one API; Python with boto3 does both, and the AWS CLI is not runnable in the agent's shell. Same API, same evidence file.
3. **Undelivered-notification alarms** on the reports and approvals topics are new (not in spec 4.10). The first manual run of Plan 1 published to a topic whose subscription was pending confirmation and nothing signalled it.
4. **A record stuck in `DECIDING`** gets a documented manual reset in the runbook, not automatic re-claiming: a second callback racing an unknown first one is worse than a manual step.

## Global Constraints

- Everything in the repo in English: code, comments, docstrings, commit messages, email text, resource names.
- Python 3.13 everywhere; Lambda `Runtime.PYTHON_3_13`, arm64.
- No em dashes in AWS resource names or descriptions; hyphens only.
- No account id, email address or secret in any file or commit. Placeholders allowed in tests, CI, snapshots and evidence: `example.com` addresses, account ids `123456789012` and `111122223333`, `<account-id>`.
- `cdk synth` and `pytest` pass with no AWS credentials; `make lint`, `make test`, `make nag` and `cfn-lint --ignore-checks W3005 -- cdk.out/*.template.json` exit 0 at the end of every task that touches CDK or Python. `make nag` and cfn-lint need `CDK_DEFAULT_ACCOUNT=123456789012 CDK_DEFAULT_REGION=us-east-1` in the environment.
- After an intended template change, regenerate the snapshot with `UPDATE_SNAPSHOTS=1 pytest tests/infra/test_snapshot.py` and read the diff before committing it.
- Fail safe at every layer, as in Plan 2: nothing in this plan loosens `dryRun`, `DELETIONS_ENABLED`, the plan integrity checks or the executor's re-verification.
- Append-only audit: run events and mirrors stay conditional puts. The approval `META` item gains one more conditional `UpdateItem` (`remindedAt`), which changes no status.
- No statement in the stack may allow an `iam:` action, a `*` action or a `service:*` action (`test_no_statement_in_the_stack_allows_iam_or_wildcard_actions` stays green).
- Unit tests use botocore `Stubber` with exact `expected_params` and call `assert_no_pending_responses()`.
- Marker constants stay in `lab_janitor/markers.py`.
- All work happens on `main` of `<repo>` (owner decision of 2026-11-02: linear history, one or more commits per phase). The old repo at `<old-repo>` changes only in Task 10 (one constant).
- Deploys and `cdk destroy` are run by the owner from their terminal. The agent verifies and runs rehearsals with boto3 from `.venv` using `profile_name="<profile>"`, region `us-east-1`. The account's Lambda concurrency quota is 10, so deploys pass `-c reservedConcurrency=false` until Task 9 lands. The owner's account already has an organization trail, so deploys pass `-c createTrail=dataEventsOnly`.
- The whole-branch review runs after Task 7 and before Task 8: nothing unreviewed is deployed.

## Review Focus

1. A stack deployed minutes ago, with no execution yet: the heartbeat alarm goes to ALARM (missing data is breaching) until the first run. Expected, documented in the runbook; and with `janitorEnabled=false` the alarm must not notify. Tests in Task 3.
2. The reminder fires after the approval was decided or expired, or a second reminder fires: no email and no write. Tests in Task 2.
3. The reminder state machine cannot be started (throttled, denied, deleted): the approval request still goes out. Test in Task 2 (catch on `StartReminder`).
4. `createTrail=none` in an account without any trail, and `dataEventsOnly` in an account that has one: the stack synthesizes and passes cdk-nag in the three modes, and `dataEventsOnly` logs no management events (no duplicate charge). Tests in Task 4.
5. A subscription is still `PendingConfirmation` when the report is published: the message reaches nobody and SNS reports no failure. The undelivered alarm (`published - delivered - failed > 0`) catches it. Test in Task 3, live check in Task 8.

---

### Task 1: Config: `createTrail`, `allowedRegions` needs `us-east-1`, strict patterns

**Files:**
- Modify: `lab_janitor/config.py`
- Test: `tests/infra/test_config.py`

**Interfaces:**
- Produces: `JanitorConfig.create_trail: str`, one of `CREATE_TRAIL_MODES = ("full", "dataEventsOnly", "none")`, default `DEFAULT_CREATE_TRAIL = "full"` (context key `createTrail`). Task 4 consumes it.

- [ ] **Step 1: Write the failing tests** (use the file's `_config(**context)` helper)

```python
@pytest.mark.parametrize("value", ["full", "dataEventsOnly", "none"])
def test_create_trail_accepts_the_three_modes(value: str):
    assert _config(createTrail=value).create_trail == value


def test_create_trail_defaults_to_full():
    assert _config().create_trail == "full"


@pytest.mark.parametrize("value", ["FULL", "data", "", True, 1])
def test_create_trail_rejects_other_values(value: object):
    with pytest.raises(ValueError, match="createTrail must be one of full, dataEventsOnly, none"):
        _config(createTrail=value)


def test_allowed_regions_must_include_us_east_1():
    with pytest.raises(ValueError, match="allowedRegions must include us-east-1"):
        _config(allowedRegions="eu-west-1,us-west-2")
    assert _config(allowedRegions="eu-west-1,us-east-1").allowed_regions == (
        "eu-west-1",
        "us-east-1",
    )


@pytest.mark.parametrize("value", ["a@example.com\n", "a@exämple.com", "a b@example.com"])
def test_email_pattern_is_ascii_and_anchored(value: str):
    with pytest.raises(ValueError, match="reportEmail must be an email address"):
        _config(reportEmail=value)


def test_region_pattern_is_ascii_and_anchored():
    with pytest.raises(ValueError, match="invalid region name"):
        _config(allowedRegions="us-east-1\n")
```

- [ ] **Step 2: Run them**

Run: `.venv/bin/python -m pytest tests/infra/test_config.py -q`
Expected: the new tests FAIL (`create_trail` missing, no `us-east-1` rule, `"a@example.com\n"` accepted because `$` matches before a trailing newline).

- [ ] **Step 3: Implement**

`_create_trail(value: Any) -> str` (same shape as `_approval_auth`). In `_regions`, after validating each name, raise `ValueError("allowedRegions must include us-east-1: CloudWatch dashboards are deleted through it")` when the result is not empty and lacks `us-east-1`. Compile `_REGION`, `_ROLE_ARN` and `_EMAIL` with `re.ASCII` and use `fullmatch` at the three call sites. Add `createTrail` to the module docstring.

- [ ] **Step 4: Run the file, then the suite and lint**

Run: `.venv/bin/python -m pytest tests/infra/test_config.py -q && make test lint`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lab_janitor/config.py tests/infra/test_config.py
git commit -m "feat(cdk): createTrail context, allowedRegions needs us-east-1, strict config patterns"
```

---

### Task 2: Approval reminder (state machine, notifier `reminder` mode)

**Files:**
- Modify: `src/shared/approvals.py`, `src/notifier/approval.py`, `src/notifier/handler.py`
- Create: `src/notifier/reminder.py`, `lab_janitor/reminder.py`
- Modify: `lab_janitor/workflow_approval.py`, `lab_janitor/workflow.py`
- Test: `tests/unit/test_approvals.py`, `tests/unit/test_notifier_reminder.py` (new), `tests/infra/test_reminder.py` (new), `tests/infra/test_workflow.py`, `tests/infra/helpers.py`
- Docs: `docs/runbook.md` (section "Approval flow")

**Interfaces:**
- Produces: `ApprovalStore.mark_reminded(approval_id: str, now: str) -> ApprovalRecord | None`.
- Produces: `notifier.approval.decision_sections(approval_url: str, approval_id: str, auth: str, confirmation: str) -> list[list[str]]` (the link block, the IAM block or both; `ValueError` on an unknown mode), used by `approval_message` and by the reminder.
- Produces: `notifier.reminder.send_reminder(event: dict, now: datetime | None = None) -> dict`; event `{"mode": "reminder", "approvalId": str}`; returns `{"reminded": True, "messageId": str}` or `{"reminded": False}`.
- Produces: `lab_janitor.reminder.REMINDER_AFTER = Duration.hours(60)` and `ApprovalReminder(scope, id, *, notifier: lambda_.IFunction)` with `.state_machine: sfn.StateMachine`.
- Produces: `Workflow.reminder_machine: sfn.StateMachine` (Task 3 consumes it) and `ApprovalBranch(..., reminder: sfn.IStateMachine)`.
- Produces in `tests/infra/helpers.py`: `state_machine_id` now returns the weekly machine (the one whose definition has a `Detect` state); new `reminder_machine_id(template) -> str` and `reminder_definition(template) -> dict`.

- [ ] **Step 1: Failing tests for `mark_reminded`** (`tests/unit/test_approvals.py`, with the file's stubbed store fixture)

`test_mark_reminded_stamps_a_pending_approval_once`: the stub expects exactly

```python
{
    "TableName": TABLE,
    "Key": serialize({"pk": f"APPROVAL#{APPROVAL_ID}", "sk": "META"}),
    "UpdateExpression": "SET remindedAt = :now",
    "ConditionExpression": "#s = :pending AND expiresAt > :now AND attribute_not_exists(remindedAt)",
    "ExpressionAttributeNames": {"#s": "status"},
    "ExpressionAttributeValues": serialize({":now": NOW_ISO, ":pending": "PENDING"}),
    "ReturnValues": "ALL_NEW",
}
```

and returns `Attributes` of a full record; the call returns an `ApprovalRecord` with that `run_id` and `expires_at`. `test_mark_reminded_returns_none_when_the_condition_fails`: `ConditionalCheckFailedException` gives `None`. `test_mark_reminded_raises_other_errors`: `ProvisionedThroughputExceededException` propagates as `ClientError`.

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/unit/test_approvals.py -q -k mark_reminded`. Expected: FAIL, `ApprovalStore` has no `mark_reminded`.

- [ ] **Step 3: Implement `mark_reminded`**, run again. Expected: PASS.

- [ ] **Step 4: Failing tests for the reminder mode** (`tests/unit/test_notifier_reminder.py`; patch `notifier.handler.build_dependencies` the way `test_notifier_approval.py` does; env `APPROVAL_URL`, `APPROVAL_AUTH`, `APPROVALS_TOPIC_ARN`)

- `test_reminder_emails_the_approvals_topic_with_the_link`: `mark_reminded` succeeds; SNS stub expects `TopicArn=APPROVALS_ARN`, `Subject="[Lab Janitor] Reminder: approval pending, run 20261102T120000Z-abc1"` and a `Message` equal to `reminder_message(record, APPROVAL_URL, "link", "abc1")`; that message contains `f"{APPROVAL_URL}/approvals/{APPROVAL_ID}"`, `"Type the confirmation code abc1"`, `f"This request expires at {record.expires_at} (UTC)"` and the plan sha256. Result `{"reminded": True, "messageId": ...}`.
- `test_reminder_is_silent_when_the_approval_is_not_pending`: the update fails its condition; no SNS response is queued; result `{"reminded": False}`; `sns` stubber has no pending responses and was never called.
- `test_reminder_in_iam_mode_has_commands_and_no_link`: with `APPROVAL_AUTH=iam` the message has `scripts/approve.py` and no `/approvals/`.
- `test_handler_dispatches_reminder_mode`: `notifier.handler.handler({"mode": "reminder", "approvalId": APPROVAL_ID}, None)` reaches `send_reminder`.
- In `test_notifier_approval.py`: `test_decision_sections_rejects_unknown_auth` (`ValueError`), and the existing message tests still pass unchanged.

- [ ] **Step 5: Run** the two notifier test files. Expected: FAIL (`notifier.reminder` missing).

- [ ] **Step 6: Implement**

`decision_sections` replaces the private `_link_lines`/`_iam_lines` use inside `approval_message` (same text, same order). `src/notifier/reminder.py`:

```python
SUBJECT_PREFIX = "[Lab Janitor] Reminder: approval pending"


def reminder_subject(record: ApprovalRecord) -> str:
    return fit_subject(f"{SUBJECT_PREFIX}, run {record.run_id}", SUBJECT_PREFIX)


def reminder_message(record: ApprovalRecord, approval_url: str, auth: str, confirmation: str) -> str:
    """First line, decision sections, expiry, sha256 and plan summary, blank-line separated."""
```

First line: `f"Reminder: the deletion plan of run {record.run_id} is still waiting for a decision."`. Expiry line: `f"This request expires at {record.expires_at} (UTC). After that nothing is deleted for this run."`. Then `f"Plan sha256: {record.plan_sha256}"` and `render_summary(record.plan_summary)`. `send_reminder` calls `mark_reminded(approval_id, now.isoformat())`; on `None` it logs `reminder skipped` with `short_hash` and returns `{"reminded": False}`; otherwise it publishes and logs the message id. Add `"reminder": reminder.send_reminder` to the handler's modes and to its docstring.

- [ ] **Step 7: Run** the unit suite. Expected: PASS.

- [ ] **Step 8: Failing infra tests**

`tests/infra/helpers.py`: change `state_machine_id` and add the two reminder helpers (Interfaces above). `tests/infra/test_reminder.py`:

- `test_there_are_two_state_machines`.
- `test_reminder_waits_then_invokes_the_notifier_in_reminder_mode`: definition `StartAt == "Wait"`; `Wait` has `SecondsPath == "$.waitSeconds"`; `Remind` is a Lambda invoke whose `Payload` is `{"mode": "reminder", "approvalId.$": "$.approvalId"}` and which retries `Lambda.TooManyRequestsException`; the last state is the `Succeed` state `Reminded`.
- `test_reminder_machine_times_out_after_61_hours`: `TimeoutSeconds == 219600`.
- `test_reminder_role_can_only_invoke_the_notifier`: the role's statements allow exactly `{"lambda:InvokeFunction"}` on the notifier function ARN (and its `:*` versions).
- `test_weekly_role_can_start_only_the_reminder`: exactly one statement with `states:StartExecution`, `Resource == {"Ref": reminder_machine_id(template)}`.

`tests/infra/test_workflow.py`: add `"StartReminder"` to `STATE_NAMES`, and

- `test_new_approval_id_leads_to_start_reminder_then_request_approval`: `NewApprovalId.Next == "StartReminder"`, `StartReminder.Next == "RequestApproval"`.
- `test_start_reminder_is_fire_and_forget`: `Resource` renders to `arn:<AWS::Partition>:states:::states:startExecution` (no `.sync`); `Parameters.Input == {"approvalId.$": "$.approvalRequest.approvalId", "runId.$": "$.detect.runId", "waitSeconds": 216000}`; `ResultPath is None`; `Catch == [{"ErrorEquals": ["States.ALL"], "ResultPath": "$.reminderError", "Next": "RequestApproval"}]`.

- [ ] **Step 9: Run** `tests/infra`. Expected: the new tests FAIL; every older test still passes once the helper selects the weekly machine.

- [ ] **Step 10: Implement the constructs**

`lab_janitor/reminder.py`: `Wait` (`sfn.WaitTime.seconds_path("$.waitSeconds")`), `Remind` (`tasks.LambdaInvoke`, `payload_response_only=True`, `retry_on_service_exceptions=True`, `add_throttle_retry`), `Reminded` (`sfn.Succeed`); `StateMachineType.STANDARD`; `timeout=REMINDER_AFTER.plus(Duration.hours(1))`; no `logs` option (the execution history is the record, and it keeps log-delivery wildcards off this role). Acknowledge on the machine `AwsSolutions-SF1` (reason: `"Three-state reminder; the execution history holds every event"`), `AwsSolutions-SF2` and `Serverless-StepFunctionStateMachineXray` (`OBSERVABILITY_REASON`), and on its role the notifier `:*` invoke finding (`wildcard_resource_finding(notifier, ":*")`).

`workflow_approval.py`: `StartReminder` is `tasks.StepFunctionsStartExecution` with `integration_pattern=sfn.IntegrationPattern.REQUEST_RESPONSE`, `input` as tested (`int(REMINDER_AFTER.to_seconds())`), `result_path=sfn.JsonPath.DISCARD`, a catch of `States.ALL` to `request_approval` with `result_path="$.reminderError"`. Chain: `new_approval_id.next(start_reminder).next(request_approval)...`. `workflow.py` builds `ApprovalReminder(self, "Reminder", notifier=notifier.function)` before the weekly machine and stores `self.reminder_machine`.

- [ ] **Step 11: Run everything and regenerate the snapshot**

Run: `make test lint && UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest tests/infra/test_snapshot.py -q && CDK_DEFAULT_ACCOUNT=123456789012 CDK_DEFAULT_REGION=us-east-1 make nag && .venv/bin/cfn-lint --ignore-checks W3005 -- cdk.out/*.template.json`
Expected: all exit 0; the snapshot diff adds one state machine, its role and policy, the `StartReminder` state and one `states:StartExecution` statement.

- [ ] **Step 12: Runbook.** In "Approval flow": the state path gains `StartReminder`; one paragraph: 60 hours after the request, if the approval is still `PENDING`, a reminder (subject `[Lab Janitor] Reminder: approval pending, run <runId>`) goes to the approvals topic with the same link or commands; it is sent at most once (`remindedAt` on the approval record); the reminder execution appears as `RUNNING` in the second state machine for 60 hours and ends without sending anything if the approval was decided.

- [ ] **Step 13: Commit**

```bash
git add src lab_janitor tests docs/runbook.md
git commit -m "feat: approval reminder 60 hours into a pending approval"
```

---

### Task 3: `Alarms` construct: heartbeat, reminder failure, undelivered notifications

**Files:**
- Create: `lab_janitor/alarms.py`
- Modify: `lab_janitor/workflow.py` (remove `_alarms`, `self.alarms`, `ALARM_PERIOD` and the unused imports), `lab_janitor/stack.py`
- Test: `tests/infra/test_alarms.py` (new; the four alarm tests and their `_alarm`/`_topic_id` helpers move here from `tests/infra/test_workflow.py`)
- Docs: `docs/runbook.md` (new section "Alarms")

**Interfaces:**
- Consumes: `Workflow.state_machine`, `Workflow.reminder_machine` (Task 2), `Notifications.{reports,approvals,alerts}_topic`, `JanitorConfig.janitor_enabled`.
- Produces: `Alarms(scope, id, *, config: JanitorConfig, state_machine: sfn.IStateMachine, reminder_machine: sfn.IStateMachine, notifications: Notifications, dlq: sqs.IQueue)` with `.alarms: list[cloudwatch.Alarm]`; the stack exposes it as `self.alarms`.

The six alarms (all with alarm and OK actions to the alerts topic):

| Construct id | Metric | Statistic, period | Condition | Missing data |
|---|---|---|---|---|
| `DlqNotEmpty` | unchanged | | | |
| `ExecutionFailed` | unchanged | | | |
| `ReminderFailed` | `reminder_machine.metric_failed()` | Sum, 5 min | `>= 1`, 1 period | notBreaching |
| `Heartbeat` | `state_machine.metric_started()` | Sum, 1 day | `< 1` for 7 of 7 periods | breaching; `actions_enabled=config.janitor_enabled` |
| `ReportsUndelivered`, `ApprovalsUndelivered` | math `published - delivered - failed` over the topic's `NumberOfMessagesPublished`, `NumberOfNotificationsDelivered`, `NumberOfNotificationsFailed` | Sum, 1 hour | `> 0`, 1 period | notBreaching |

Metric math arithmetic treats a missing value as 0, so a publish with no delivery datapoint yields a positive result. A failed delivery is already covered by the subscription redrive to the DLQ, which is why `failed` is subtracted.

- [ ] **Step 1: Move the alarm tests** to `tests/infra/test_alarms.py` unchanged except `test_alarms` (count becomes 6). Add:

```python
def test_heartbeat_alarm_is_seven_daily_periods_breaching(template):
    alarm = _alarm(template, "ExecutionsStarted")
    assert alarm["Namespace"] == "AWS/States"
    assert alarm["Dimensions"] == [
        {"Name": "StateMachineArn", "Value": {"Ref": state_machine_id(template)}}
    ]
    assert (alarm["Statistic"], alarm["Period"]) == ("Sum", 86400)
    assert (alarm["EvaluationPeriods"], alarm["DatapointsToAlarm"]) == (7, 7)
    assert (alarm["Threshold"], alarm["ComparisonOperator"]) == (1, "LessThanThreshold")
    assert alarm["TreatMissingData"] == "breaching"


def test_heartbeat_actions_follow_janitor_enabled(template, live_template):
    assert _alarm(template, "ExecutionsStarted").get("ActionsEnabled", True) is True
    assert _alarm(live_template, "ExecutionsStarted")["ActionsEnabled"] is False
```

plus `test_reminder_failed_alarm` (namespace `AWS/States`, `ExecutionsFailed`, dimension `Ref` of `reminder_machine_id(template)`, Sum, 300, `>= 1`, notBreaching), `test_undelivered_alarms_cover_reports_and_approvals_only` (two alarms whose expression is `published - delivered - failed`; their three metrics are the SNS ones with `Stat == "Sum"` and `Period == 3600`; the `TopicName` dimensions are the reports and approvals topics; threshold 0, `GreaterThanThreshold`, notBreaching; no such alarm on the alerts topic) and keep `test_alarms_notify_alerts_on_alarm_and_ok` over all six. `_alarm(template, needle)` must keep finding one alarm by metric name or expression; give it an optional `dimension_ref` argument for the two `ExecutionsFailed`-based alarms.

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/infra/test_alarms.py -q`. Expected: FAIL (count 2, no heartbeat).

- [ ] **Step 3: Implement** `lab_janitor/alarms.py` (move the two existing alarms verbatim, add the four new ones; one private method per alarm family), remove the alarm code from `workflow.py`, create `Alarms(self, "Alarms", ...)` in the stack after the workflow. Descriptions, hyphens only: `"Lab janitor: no execution started in the last 7 days"`, `"Lab janitor: an approval reminder failed"`, `"Lab janitor: a report was published but reached no subscriber"`, `"Lab janitor: an approval request was published but reached no subscriber"`.

- [ ] **Step 4: Run, snapshot, nag, cfn-lint** (same command as Task 2 Step 11). Expected: exit 0; the snapshot diff shows the two alarms under new logical ids and four new alarms.

- [ ] **Step 5: Runbook section "Alarms".** One row per alarm: what it means, what to do. Include: the heartbeat fires once on a brand new stack until the first execution (start a manual dry-run after deploying); a paused janitor (`janitorEnabled=false`) keeps the alarm but disables its actions; an undelivered alarm almost always means a subscription is `PendingConfirmation` (how to list them is already in "Deploy in dry-run"); the alerts topic cannot alarm on itself, so confirm its subscription first; DLQ inspection and purge (`receive_message` with `VisibilityTimeout=0`, then `purge_queue`) as `python -c` snippets in the runbook's existing style; moving the alarms changed their names once on this deploy.

- [ ] **Step 6: Commit**

```bash
git add lab_janitor tests docs/runbook.md
git commit -m "feat(cdk): alarms construct with heartbeat, reminder and undelivered-notification alarms"
```

---

### Task 4: CloudTrail data events (`createTrail`)

**Files:**
- Create: `lab_janitor/trail.py`
- Modify: `lab_janitor/stack.py`, `lab_janitor/audit.py` (the `AwsSolutions-S1` reason)
- Test: `tests/infra/test_trail.py` (new), `tests/infra/conftest.py` (fixtures `data_only_template`: `createTrail=dataEventsOnly`; `no_trail_template`: `createTrail=none`)
- Docs: `docs/runbook.md` (section "CloudTrail baseline" becomes "CloudTrail")

**Interfaces:**
- Consumes: `JanitorConfig.create_trail` (Task 1), `AuditStore.plans_bucket`, `AuditStore.table`, `audit.PLANS_PREFIX`.
- Produces: `AuditTrail(scope, id, *, mode: str, audit: AuditStore)` with `.log_bucket: s3.Bucket` and `.trail: cloudtrail.CfnTrail`; the stack creates it as `"Trail"` unless the mode is `none` (`self.trail: AuditTrail | None`) and adds the output `TrailLogBucketName` when it exists.

Resource shape:

- Log bucket: versioned, `BLOCK_ALL`, `BUCKET_OWNER_ENFORCED`, `enforce_ssl=True`, `RemovalPolicy.RETAIN`, one lifecycle rule (expiration 400 days, noncurrent versions 30 days).
- Bucket policy, both statements for `iam.ServicePrincipal("cloudtrail.amazonaws.com")` and both with `StringEquals aws:SourceArn = <trail ARN>`: `s3:GetBucketAcl` on the bucket; `s3:PutObject` on `<bucket>/AWSLogs/<account>/*` with `s3:x-amz-acl = bucket-owner-full-control` as well.
- `CfnTrail`: `trail_name=f"{stack.stack_name}-audit"` (the ARN `arn:<partition>:cloudtrail:<region>:<account>:trail/<name>` is needed by the bucket policy before the trail exists), `is_logging=True`, `enable_log_file_validation=True`, `is_multi_region_trail` and `include_global_service_events` both `mode == "full"`, `s3_bucket_name`, and `advanced_event_selectors` in this order:
  1. only in `full`: name `Management events`, field `eventCategory` equals `Management`;
  2. name `Plan objects`: `eventCategory` equals `Data`, `resources.type` equals `AWS::S3::Object`, `resources.ARN` starts with `<plans bucket ARN>/plans/`;
  3. name `Audit table items`: `eventCategory` equals `Data`, `resources.type` equals `AWS::DynamoDB::Table`, `resources.ARN` equals the table ARN.
- The trail depends on the bucket policy (`trail.node.add_dependency(policy)`), or CloudTrail rejects the bucket.
- Acknowledge `AwsSolutions-S1` on the log bucket: `"CloudTrail log destination; server access logs here would only log the log deliveries"`. The plans bucket reason becomes `"Append-only versioned bucket; object-level access is recorded by CloudTrail data events unless createTrail is none"`.

- [ ] **Step 1: Failing tests** (`tests/infra/test_trail.py`)

- `test_default_mode_is_a_multi_region_trail_with_management_and_data_events(template)`: one `AWS::CloudTrail::Trail`; `IsMultiRegionTrail`, `IncludeGlobalServiceEvents`, `EnableLogFileValidation`, `IsLogging` all true; selector names `["Management events", "Plan objects", "Audit table items"]`.
- `test_data_events_only_logs_no_management_events(data_only_template)`: `IsMultiRegionTrail` false; selector names `["Plan objects", "Audit table items"]`; no field selector anywhere equals `Management`.
- `test_data_selectors_pin_the_plans_prefix_and_the_audit_table(data_only_template)`: the S3 selector's `resources.ARN` `StartsWith` renders (helper `render`) to `<plans bucket Arn>/plans/`; the DynamoDB selector's `Equals` is `[{"Fn::GetAtt": [<table id>, "Arn"]}]`.
- `test_none_creates_no_trail_and_no_log_bucket(no_trail_template)`: zero trails, exactly one `AWS::S3::Bucket`, no `TrailLogBucketName` output.
- `test_log_bucket_is_private_versioned_and_retained(template)`: versioning enabled, the four public access blocks true, `BucketOwnerEnforced`, `DeletionPolicy == "Retain"`, lifecycle `ExpirationInDays == 400` and `NoncurrentVersionExpiration.NoncurrentDays == 30`.
- `test_log_bucket_policy_allows_only_this_trail(template)`: the two Allow statements for `cloudtrail.amazonaws.com` each carry `aws:SourceArn` rendering to `arn:<AWS::Partition>:cloudtrail:us-east-1:123456789012:trail/LabJanitorStack-audit`; the `PutObject` one also `s3:x-amz-acl`; apart from the TLS Deny there is no other statement.
- `test_trail_waits_for_the_bucket_policy(template)`: the trail's `DependsOn` holds the log bucket policy's logical id.
- `test_three_modes_pass_synthesis(template, data_only_template, no_trail_template)`: each has the `StateMachineArn` output (a smoke check that nothing else depends on the trail).

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/infra/test_trail.py -q`. Expected: FAIL (no trail resource).

- [ ] **Step 3: Implement** `lab_janitor/trail.py` and wire it in the stack. Fix any older test that assumed a single bucket or a single bucket policy by selecting the plans bucket explicitly (same assertion, narrower selection).

- [ ] **Step 4: Run, snapshot, nag in the three modes, cfn-lint**

Run: `make test lint && UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest tests/infra/test_snapshot.py -q`, then for each of `full`, `dataEventsOnly`, `none`: `CDK_DEFAULT_ACCOUNT=123456789012 CDK_DEFAULT_REGION=us-east-1 npx cdk synth --strict --quiet -c reportEmail=ci@example.com -c createTrail=<mode> && .venv/bin/cfn-lint --ignore-checks W3005 -- cdk.out/*.template.json`
Expected: exit 0 three times.

- [ ] **Step 5: Runbook section "CloudTrail".** Keep the baseline check. Add: the three modes and when to use each (`full` in an account with no trail; `dataEventsOnly` when a trail already delivers management events, as in the owner's account; `none` to skip it); cost note (data events are billed per event, a weekly run writes a few dozen); where the files land (`s3://<TrailLogBucketName>/AWSLogs/<account-id>/CloudTrail/<region>/yyyy/mm/dd/`, delivered within about 5 to 15 minutes); a `python -c`-style snippet that lists the day's files, gunzips them and prints `eventTime`, `eventName`, the role session name and the resource ARN of every record whose `requestParameters` mention a given run id; and the management-event lookup for the executor: `cloudtrail.lookup_events(LookupAttributes=[{"AttributeKey": "Username", "AttributeValue": "<executor function name>"}])` (the session name of a Lambda role is the function name).

- [ ] **Step 6: Commit**

```bash
git add lab_janitor tests docs/runbook.md
git commit -m "feat(cdk): CloudTrail trail for the audit trail's data events, selected by createTrail"
```

---

### Task 5: IAM simulation of the executor policy

**Files:**
- Create: `tests/iam/__init__.py`, `tests/iam/simulate_policies.py`, `docs/evidence/iam-simulation.txt` (generated)
- Test: `tests/infra/test_simulate_policies.py` (new; uses the `cli_template` fixture)
- Modify: `lab_janitor/executor.py` (`LAB_NAME_LOCK_REASON`), `Makefile` (target `simulate`)
- Docs: `docs/runbook.md` (short section "IAM simulation")

**Interfaces:**
- Produces, in `tests/iam/simulate_policies.py`:
  - `ACCOUNT = "123456789012"`, `REGION = "us-east-1"`, `TABLE_ARN = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/audit"`, `PLANS_BUCKET_ARN = "arn:aws:s3:::plans"`.
  - `role_policy(template: dict, handler: str) -> dict`: one policy document with every statement of the `AWS::IAM::Policy` resources attached to the execution role of the function whose `Handler` is `handler`, intrinsics resolved (`AWS::Partition` to `aws`, `AWS::AccountId` to `ACCOUNT`, `AWS::Region` to `REGION`; `Fn::GetAtt [X, "Arn"]` to `TABLE_ARN` for a DynamoDB table, `PLANS_BUCKET_ARN` for a bucket, `arn:aws:simulated:::<X>` otherwise; `Fn::Join` joined).
  - `@dataclass(frozen=True) class Case: name: str; action: str; resource: str; expected: str; tags: dict[str, str] = {}; leading_keys: tuple[str, ...] = ()`.
  - `CASES: tuple[Case, ...]` (table below), `evaluate(iam, policy: dict, case: Case) -> str` (the `EvalDecision` of one `simulate_custom_policy` call), `render(results: list[tuple[Case, str]]) -> str`, `main(argv: list[str] | None = None) -> int` with `--template` (default `cdk.out/LabJanitorStack.template.json`), `--out` (default `docs/evidence/iam-simulation.txt`) and `--profile`; exit 1 if any case differs.

The cases (resources use `ACCOUNT` and `REGION`; "lab" means tag `Purpose=lab`, "keep" means tag `janitor=keep`):

| Action | Resource | Tags | Expected |
|---|---|---|---|
| `cloudwatch:DeleteDashboards` | `dashboard/LAB-demo` | none | `allowed` |
| `cloudwatch:DeleteDashboards` | `dashboard/PROD-demo` | none | `implicitDeny` |
| `cloudwatch:DeleteDashboards` | `dashboard/LAB-demo` | keep | `explicitDeny` |
| `internetmonitor:DeleteMonitor` | `monitor/LAB-demo` | none | `allowed` |
| `internetmonitor:DeleteMonitor` | `monitor/PROD-demo` | none | `implicitDeny` |
| `internetmonitor:DeleteMonitor` | `monitor/LAB-demo` | keep | `explicitDeny` |
| `internetmonitor:UpdateMonitor` | `monitor/LAB-demo` | keep | `explicitDeny` |
| `ec2:DeleteSnapshot` | `snapshot/snap-0123456789abcdef0` | lab | `allowed` |
| `ec2:DeleteSnapshot` | same | none | `implicitDeny` |
| `ec2:DeleteSnapshot` | same | lab, keep | `explicitDeny` |
| `ec2:DeleteVolume` | `volume/vol-0123456789abcdef0` | lab | `allowed` |
| `ec2:DeleteVolume` | same | none | `implicitDeny` |
| `ec2:ReleaseAddress` | `elastic-ip/eipalloc-0123456789abcdef0` | lab | `allowed` |
| `ec2:ReleaseAddress` | same | none | `implicitDeny` |
| `ec2:ReleaseAddress` | same | lab, keep | `explicitDeny` |
| `ec2:TerminateInstances` | `instance/i-0123456789abcdef0` | lab | `implicitDeny` |
| `ec2:CreateTags` | `snapshot/snap-0123456789abcdef0` | lab | `implicitDeny` |
| `s3:DeleteBucket` | `arn:aws:s3:::plans` | none | `implicitDeny` |
| `s3:DeleteObject` | `arn:aws:s3:::plans/plans/run/plan.json` | none | `implicitDeny` |
| `rds:DeleteDBInstance` | `arn:aws:rds:us-east-1:123456789012:db:demo` | lab | `implicitDeny` |
| `dynamodb:PutItem` | `TABLE_ARN` | leading key `RUN#20261110T140012Z-7f3a` | `allowed` |
| `dynamodb:PutItem` | `TABLE_ARN` | leading key `APPROVAL#abc` | `implicitDeny` |

Tags become `ContextEntries` of type `string` named `aws:ResourceTag/<key>`; leading keys become one `stringList` entry named `dynamodb:LeadingKeys`.

- [ ] **Step 1: Failing tests** (`tests/infra/test_simulate_policies.py`)

- `test_role_policy_resolves_every_intrinsic(cli_template)`: `json.dumps(role_policy(cli_template, "executor.handler.handler"))` contains neither `"Fn::"` nor `"Ref"`, and does contain `arn:aws:cloudwatch::123456789012:dashboard/LAB-*` and the Sid `ExecutorDenyKept`.
- `test_cases_cover_the_spec_matrix()`: the set of `(action, expected)` pairs includes an `allowed` and an `explicitDeny` for each of the five delete actions, and `implicitDeny` for `ec2:TerminateInstances`, `s3:DeleteBucket` and `ec2:CreateTags`; names are unique.
- `test_evaluate_sends_tags_and_leading_keys_as_context(...)`: with an `iam` Stubber, `expected_params` holds `PolicyInputList=[json.dumps(policy)]`, `ActionNames`, `ResourceArns` and the two kinds of `ContextEntries`; the stubbed `EvalDecision` is returned.
- `test_main_writes_the_report_and_fails_on_a_mismatch(tmp_path, monkeypatch)`: with `evaluate` patched to answer `allowed` for everything, `main` returns 1, and the file has one line per case with `PASS` or `FAIL`.

- [ ] **Step 2: Run** them. Expected: FAIL (`tests.iam.simulate_policies` missing).

- [ ] **Step 3: Implement** the script. The report is plain text: a header with the date and the template path, then one line per case: `PASS|FAIL  <action>  <resource>  tags=<...>  expected=<...>  actual=<...>`, then a totals line. No account id other than `ACCOUNT`. `make simulate` runs `python tests/iam/simulate_policies.py`.

- [ ] **Step 4: Run** the tests and lint. Expected: PASS.

- [ ] **Step 5: Run it for real** (needs credentials; `iam:SimulateCustomPolicy` is read-only)

Run: `CDK_DEFAULT_ACCOUNT=123456789012 CDK_DEFAULT_REGION=us-east-1 make synth && .venv/bin/python tests/iam/simulate_policies.py --profile <profile>`
Expected: exit 0, 22 `PASS` lines in `docs/evidence/iam-simulation.txt`. A `FAIL` is a finding about the policy, not about the script: stop and fix the policy under systematic-debugging.

- [ ] **Step 6: The keep-tag reason.** The Service Authorization Reference lists `aws:ResourceTag/${TagKey}` for the `dashboard` resource type of CloudWatch and the `Monitor` resource type of Internet Monitor, the resource types of `DeleteDashboards` and of `UpdateMonitor`/`DeleteMonitor` (checked 2026-11-11). Replace the parenthesis in `LAB_NAME_LOCK_REASON` with: `"kept resources are refused by the keep-tag Deny (both resource types support aws:ResourceTag) and again by the executor's re-verification"`. Runbook: three lines on what the simulation proves (policy logic) and does not (that a service sends the tag key at run time; that comes from the reference above), and how to rerun it.

- [ ] **Step 7: Commit**

```bash
git add tests/iam tests/infra/test_simulate_policies.py docs/evidence/iam-simulation.txt lab_janitor/executor.py Makefile docs/runbook.md tests/snapshots
git commit -m "test(iam): simulate the executor policy and commit the result as evidence"
```

---

### Task 6: Approval path hardening (Plan 2 review residuals)

**Files:**
- Modify: `src/shared/approvals.py`, `src/approval/decision.py`, `src/approval/handler.py`, `src/notifier/approval.py`, `src/notifier/outcomes.py`, `src/executor/verify.py`, `lab_janitor/approval_api.py`, `scripts/approve.py`
- Test: `tests/unit/test_approvals.py`, `tests/unit/test_approval_handler.py`, `tests/unit/test_notifier_approval.py`, `tests/unit/test_notifier_outcomes.py`, `tests/unit/test_executor_verify.py`, `tests/infra/test_approval_api.py`, `tests/unit/test_approve_script.py`
- Docs: `docs/runbook.md` (status table; "A decision left in DECIDING")

**Interfaces:**
- Produces: `AlreadyDecidedError.status: str | None` (the status the failed condition saw; `None` when unknown).
- Produces: HTTP status `504` from `decide` when the callback outcome is unknown; `STATUS_PAGES[504] = ("Decision status unknown", "The decision may have been recorded. Check the latest emails for this run before trying again.")`, `ERROR_NAMES[504] = "unknown outcome"`.
- Produces: `notifier.report.count_noun(count: int, noun: str) -> str` (`"1 deletion"`, `"2 deletions"`).
- Produces: `scripts.approve.region_from_url(url: str) -> str | None`.

Each bullet is one RED-GREEN cycle: write the named test, watch it fail, implement, watch it pass.

- [ ] **Step 1: `410` when the state machine expired the approval first.** `test_claim_reports_the_status_it_lost_to` (store: the conditional failure's old item has `status` `EXPIRED`; the raised `AlreadyDecidedError.status == "EXPIRED"`). `test_decision_on_a_just_expired_record_returns_410` (handler: the record is read `PENDING`, the claim fails against an `EXPIRED` old item; status 410, no Step Functions call). `decide` uses `error.status`, not the earlier read.

- [ ] **Step 2: A `BotoCoreError` in `finalize` does not undo a delivered decision.** `test_finalize_read_timeout_still_returns_200` (callback accepted, `finalize` raises `ReadTimeoutError`; status 200; one `ERROR` log line without the approval id).

- [ ] **Step 3: Unknown callback outcome is `504`, not `500`.** `test_unknown_callback_outcome_returns_504_and_keeps_deciding` (link route: HTML page with the `504` title; IAM route: `{"error": "unknown outcome"}`; the record stays `DECIDING`; no revert call). The existing "500 after BotoCoreError" tests change to 504.

- [ ] **Step 4: Singular counts.** `test_subject_and_body_use_singular_for_one_candidate`: subject `[Lab Janitor] Approval needed: 1 deletion, ~$0.00/month, run <id>`; body first line `Lab Janitor wants to delete 1 resource (~$0.00/month).`; two candidates keep the plural.

- [ ] **Step 5: The result email is bounded.** `test_result_with_thousands_of_items_stays_under_the_email_limit`: an `apply` with 5,000 items produces a `Message` of at most `REPORT_EMAIL_LIMIT_BYTES` bytes whose end is the truncation pointer to `s3://<bucket>/plans/<runId>/plan.json`; a small result is unchanged. Use `shared.report.truncate_report`.

- [ ] **Step 6: Honest reason for an unknown age.** `test_missing_timestamp_reads_age_unknown` (`tests/unit/test_executor_verify.py`): a snapshot or volume without its timestamp is skipped with the reason `age unknown`, not `below minimum age (None days)`. Add one docstring sentence to the EIP verifier: its age is not re-queried at apply time because nothing in the audit table can change between detection and apply.

- [ ] **Step 7: The HTTP API has a name.** `test_api_is_named_after_the_stack`: `Name == "LabJanitorStack-approvals"`. Implement with `api_name=f"{Stack.of(self).stack_name}-approvals"`.

- [ ] **Step 8: The CLI signs for the API's region.** `test_region_from_url` (`https://abc123.execute-api.eu-west-1.amazonaws.com` gives `eu-west-1`; a custom domain gives `None`); `test_region_defaults_to_the_api_host` (no `--region`, profile region `us-east-1`, URL in `eu-west-1`: the request is signed for `eu-west-1`); `test_http_error_prints_status_and_body_and_exits_1`; `test_url_error_exits_1`. Order of precedence: `--region`, then the URL, then the profile.

- [ ] **Step 9: Runbook.** Status table: change the `500` meaning to "The decision was not recorded (the claim was reverted); retry." and add `504`: "Step Functions did not answer; the decision may or may not have been delivered. The record stays `DECIDING`. Check for the result or rejection email; if the run is still waiting after a few minutes, reset the record (below) and decide again." New subsection "A decision left in DECIDING" with the `python -c` reset: `update_item` on `APPROVAL#<approvalId>`/`META`, `SET #s = :pending REMOVE approvedBy`, condition `#s = :deciding`, run from an administrator session, and the warning to do it only while the execution still shows `RequestApproval` as running.

- [ ] **Step 10: Suite, lint, snapshot, nag, cfn-lint, commit**

```bash
git add src lab_janitor scripts tests docs/runbook.md
git commit -m "fix: approval path residuals from the Plan 2 review"
```

---

### Task 7: Detector time budget and the missing tests

**Files:**
- Modify: `src/detector/detectors/ec2.py`, `elb.py`, `databases.py`, `global_.py`, `monitor.py`
- Test: `tests/unit/test_detect_ec2.py`, `tests/unit/test_detect_databases.py`, `tests/unit/test_settings.py`, `tests/unit/test_marking.py`, `tests/unit/test_aws_clients.py`, `tests/unit/test_report.py`, `tests/unit/test_detector_handler.py`, `tests/unit/test_notifier_handler.py`, `tests/infra/test_iam_guardrails.py`

**Interfaces:** none new.

- [ ] **Step 1: Mid-listing deadline (RED).** `test_snapshot_listing_stops_at_the_deadline_between_pages` in `tests/unit/test_detect_ec2.py`: `describe_snapshots` answers a first page with a `NextToken`; the context's deadline passes before the second page (patch `time.time` in `detector.sweep`); no second response is queued; the errors list holds one `time-budget` error for `ec2:DescribeSnapshots` and no snapshot finding is emitted. Expected today: FAIL (the second page is requested).

- [ ] **Step 2: Pass `deadline=ctx.deadline` to every `collect` call** that lacks it, in the five detector modules (`describe_volumes` already has it). Run the detector tests. Expected: PASS, including Step 1.

- [ ] **Step 3: ElastiCache cluster without an ARN (RED-GREEN).** `test_cache_cluster_without_arn_is_reported_with_unknown_tags` in `tests/unit/test_detect_databases.py`: a `describe_cache_clusters` item with no `ARN` yields a finding (never a candidate), no `list_tags_for_resource` call and no exception. Implement with `.get("ARN")`.

- [ ] **Step 4: Tests that pin existing behavior.** These pass on first run, so each must be seen to fail once: break the line it pins, watch the test fail, restore the line.

- `tests/unit/test_settings.py`: `test_prefix_shorter_than_three_is_rejected` (`LAB_NAME_PREFIX="LA"`, `ValueError` matching `at least 3`); `test_each_empty_marker_is_rejected` (parametrized over `LAB_TAG_KEY`, `LAB_TAG_VALUE`, `KEEP_TAG_KEY`, `KEEP_TAG_VALUE`; the message names the variable); `test_region_falls_back_to_us_east_1` (no `AWS_REGION`).
- `tests/unit/test_marking.py`: `test_lowercase_tag_shape_is_read` (`[{"key": "Purpose", "value": "lab"}]` is lab-tagged; `[{"key": "janitor", "value": "keep"}]` is kept); `test_is_kept_negatives` (`None`, `[]`, value `Keep`, key `Janitor`, the lab tag alone: all `False`).
- `tests/unit/test_report.py`: `test_finding_and_error_to_dict_shapes` (exact key sets; no `tags` key).
- `tests/unit/test_aws_clients.py`: `test_enabled_regions_are_sorted` (Stubber: `describe_regions` with `{"AllRegions": False}` returning `us-west-2`, `eu-west-1`; result `["eu-west-1", "us-west-2"]`).
- `tests/unit/test_detector_handler.py`: `test_main_prints_a_plan_and_never_writes` (`start_run` and `detect_plan` patched; `build_dependencies`, `PlanStore` and `AuditClient` patched to raise if touched; stdout parses as JSON with the run id).
- `tests/unit/test_notifier_handler.py`: `test_fit_subject_keeps_99_characters_and_falls_back_at_100`.
- `tests/infra/test_iam_guardrails.py`: `test_keep_deny_is_pinned` (the `ExecutorDenyKept` statement: `Effect` Deny, `Resource == "*"`, actions exactly `EXECUTOR_MUTATING_ACTIONS`, condition exactly `{"StringEquals": {"aws:ResourceTag/janitor": "keep"}}`); `test_no_policy_statement_uses_not_action_or_not_resource` (over `_all_policy_statements(any_template)`); `test_function_roles_carry_only_the_basic_execution_managed_policy` (parametrized over the four handlers: one managed policy, rendering ends with `service-role/AWSLambdaBasicExecutionRole`).

- [ ] **Step 5: Suite and lint, commit**

```bash
git add src tests
git commit -m "test: detector time budget on every listing and the tests deferred from Plan 2"
```

---

### Task 8: Deploy in dry-run and rehearse the operational paths (owner and agent)

**Files:**
- Modify: `docs/runbook.md` (rehearsal record), `docs/superpowers/plans/2026-11-03-plan-1-followups.md`
- Create: `docs/evidence/alarm-history-<date>.json` (redacted)

**Interfaces:**
- Consumes: everything above, reviewed. Produces: a stack on the Plan 3 template, still in dry-run, with the reject, wrong-code, reminder, failure-alarm and data-event paths seen working.

- [ ] **Step 1: Owner deploys** (the agent prints the command and waits):

```bash
cd <repo> && export AWS_PROFILE=<profile> AWS_REGION=us-east-1 CDK_DEFAULT_ACCOUNT=<account-id> CDK_DEFAULT_REGION=us-east-1 PATH="$PWD/.venv/bin:$PATH" && npx cdk deploy LabJanitorStack --require-approval broadening -c reservedConcurrency=false -c createTrail=dataEventsOnly -c reportEmail=<owner> -c alertEmail=<owner>
```

Expected: `UPDATE_COMPLETE`; the diff shows the trail, its bucket, the reminder machine and six alarms.

- [ ] **Step 2: Agent verifies (read-only).** Stack status and `LastUpdatedTime` of today; six alarms, none in `ALARM` after ten minutes (the heartbeat is `OK` because of the runs of the last seven days); `get_trail_status` shows `IsLogging`; `get_event_selectors` shows the two data selectors and no management selector; two state machines; executor still `DELETIONS_ENABLED=false`; three subscriptions confirmed.

- [ ] **Step 3: Reject rehearsal.** Agent creates the decoy dashboard `LAB-decoy-plan3` (empty body) and starts `{"dryRun": false, "minAgeDays": 7}`. Expected: the run waits in `RequestApproval`; `StartReminder` succeeded and one reminder execution is `RUNNING` with `waitSeconds` 216000. Agent starts a second reminder execution by hand with `{"approvalId": "<id>", "runId": "<runId>", "waitSeconds": 5}`: the reminder email arrives, the record gains `remindedAt`, and a third hand-started one ends with `{"reminded": false}`. Agent stops the 60-hour reminder execution. Owner opens the link, types a wrong code (expect the `400` page; the agent confirms the record is still `PENDING` and the access log shows `400`), then the right code and **Reject**, then submits again (expect `409`). Agent verifies: path `RecordRejected`, `MirrorRejected`, `NotifyRejected`, `Rejected`; `META` is `REJECTED`; the mirror event is `REJECTED`; the executor was not invoked; the rejection email was delivered (SNS `NumberOfNotificationsDelivered`).

- [ ] **Step 4: Failure-path rehearsal.** Agent starts an execution whose input is the JSON array `[]`. Expected: `Defaults` fails (a `ResultPath` cannot be applied to an array), the execution ends `FAILED` without reaching `NotifyFailure`, the `ExecutionFailed` alarm goes to `ALARM` within five minutes and back to `OK` afterwards, and the alerts mailbox gets both emails. Agent saves `describe_alarm_history` for that alarm (account id redacted) as evidence. If the execution does not fail on `[]`, record what happened and use `{"dryRun": false, "minAgeDays": "x"}` only after reading what the detector does with it; do not improvise other inputs.

- [ ] **Step 5: Data events.** Fifteen minutes after Step 3, the agent lists the trail bucket for today and finds at least one `PutObject` on `plans/<runId>/plan.json` by the detector role and one `PutItem` on the audit table. Record the two event names and times.

- [ ] **Step 6: Clean up and record.** Agent deletes `LAB-decoy-plan3` after checking its body is still `{"widgets":[]}`. A "Rehearsal record" subsection in the runbook (run ids, approval ids, what was seen; no email, no account id, no IP), defects to the follow-ups file. Commit:

```bash
git add docs
git commit -m "docs: operational rehearsal of the reject, reminder, failure and CloudTrail paths"
```

---

### Task 9: Lambda concurrency quota (owner)

- [ ] **Step 1: Owner requests the increase** (the agent prints it; the request is the owner's to make):

```bash
aws service-quotas request-service-quota-increase --service-code lambda --quota-code L-B99A9384 --desired-value 1000 --profile <profile> --region us-east-1
```

- [ ] **Step 2:** When `get_service_quota` shows at least 102, every later deploy drops `-c reservedConcurrency=false`, and the agent verifies `ReservedConcurrentExecutions == 1` on the detector and the executor. This does not block Tasks 10 to 12: without the reservation a retried `Apply` could overlap, and both executors re-verify every item and write conditionally (runbook, "Deploy in dry-run").

---

### Task 10: Cutover (owner and agent)

**Files:**
- Modify (old repo): `<old-repo>/aws_janitor/aws_janitor_stack.py` (`DRY_RUN = "true"`)
- Modify: `docs/runbook.md` (header paragraph, "Cutover record")

- [ ] **Step 1: Old stack to dry-run.** Agent changes the constant and commits in the old repo. Owner deploys it:

```bash
cd <old-repo> && source .venv/bin/activate && AWS_PROFILE=<profile> cdk deploy AwsJanitorStack
```

Agent verifies the old function's environment has `DRY_RUN=true`.

- [ ] **Step 2: New stack live.** Owner deploys with the Task 8 command plus `-c dryRun=false`. Agent verifies: executor `DELETIONS_ENABLED=true`; the schedule's input has `"dryRun":false`; alarms unchanged.

- [ ] **Step 3: Runbook.** The opening paragraph no longer says deletions wait for Plan 3; it states the two switches (`dryRun` context and the execution input). Commit with Task 11's evidence.

---

### Task 11: Controlled deletion test (spec section 6; owner and agent)

**Files:**
- Create: `docs/evidence/controlled-deletion-<date>.md`, `docs/evidence/controlled-deletion-<date>-audit.json`

- [ ] **Step 1: Preview.** On a day other than Monday the agent starts `{"dryRun": true, "minAgeDays": 0}` and reads the plan. Expected: zero candidates. Anything proposed here is a real resource of the owner: stop and show it to the owner before going on.

- [ ] **Step 2: Decoys** (agent, us-east-1): dashboard `LAB-decoy`; a 1 GiB gp3 volume tagged `Purpose=lab`; snapshot A of it tagged `Purpose=lab`; snapshot B of it tagged `Purpose=lab` and `janitor=keep`. Wait until both snapshots are `completed`.

- [ ] **Step 3: Live run.** Agent starts `{"dryRun": false, "minAgeDays": 0}`. Expected plan: exactly three candidates (the dashboard, the volume, snapshot A); snapshot B in the inventory with the reason `tagged janitor=keep`. If the approval email lists anything else, the owner rejects and the test stops.

- [ ] **Step 4: Owner approves** through the link with the confirmation code.

- [ ] **Step 5: Agent verifies.** Result `3 deleted, 0 failed, 0 skipped`; three `DELETED` audit items by `lambda:executor` and the `APPROVED` mirror; the dashboard, the volume and snapshot A no longer exist; snapshot B exists; the result email was delivered; `lookup_events` shows `DeleteDashboards`, `DeleteVolume` and `DeleteSnapshot` under the executor's session name; the trail bucket has the `PutItem` data events of the three outcomes; a second `POST` on the approval returns `409`.

- [ ] **Step 6: Clean up.** Agent deletes snapshot B after checking its id and tags.

- [ ] **Step 7: Evidence.** The markdown file: date, run id, approval id, the plan summary, the three outcomes, the CloudTrail rows (event name, time, session name, resource), what the keep-tagged snapshot showed. The JSON file: the `RUN#<runId>` items with `approvedBy` reduced to its `mode` and `at`. No account id, IP, user agent or email in either. The owner adds redacted screenshots of the approval and result emails. Commit:

```bash
git add docs
git commit -m "docs: controlled deletion test and cutover record"
```

---

### Task 12: Retire the old stack (owner)

- [ ] **Step 1: Owner destroys it** after Task 11 passed:

```bash
cd <old-repo> && source .venv/bin/activate && AWS_PROFILE=<profile> cdk destroy AwsJanitorStack
```

- [ ] **Step 2: Agent lists what the stack left behind** (its retained log group, by name). The owner deletes it; the agent confirms that the stack, its schedule, its topic and the log group are gone and that `LabJanitorStack` is unaffected.

- [ ] **Step 3: Close out.** Follow-ups file: move what this plan closed to "Done in Plan 3", keep the "Left open" list below for Plan 4. Commit.

---

## Left open (not in this plan)

- The four tag-lookup variants of the detector still do not share `read_tags`; the "AMI-backed" reason is only set for lab snapshots; the unreachable `attached` reason; `global` as a service label; audit warnings without `runId`. Cosmetic; no behavior a user sees.
- Notifier: config validation after `approvals.create`, the later expiry in a retried email, `_one_line` duplicated, clients built twice. Approval handler: clients built per request. Executor: `_error_code` duplicated; a dashboard item with a `None` ARN fails safe.
- CDK: `APPROVAL_KEYS` versus the inline `RUN#*` condition; monitor action literals repeated; the policy scan does not cover KMS key policies or `Lambda::Permission` (none present).
- The deployed commit is not shown in the report; a stale deployment is caught by the post-deploy check in Task 8 Step 2, not by an alarm.
- The IAM approval route is not exercised live (it needs `approvalAuth=both` and an approver role in the deploy command). Offer it to the owner after Task 11.
- Plan 4: README, ADRs, threat model (it must mention the approval id in the reminder execution's input), spec text updates, commit author identity, publication.

## Self-review notes

- Spec coverage: 4.5 external evidence and `createTrail` (Tasks 1, 4); 4.10 heartbeat (Task 3), approval reminder (Task 2, as an email: decision 1), DLQ and execution-failed alarms (moved, Task 3); 5.3 `createTrail` (Task 1); 5.4 IAM simulation (Task 5) and the heartbeat infra gate (Task 3); 5.5 runbook items: alarms and DLQ (Task 3), late approval and pause (already present), one-off runs (present), audit table and CloudTrail queries (Tasks 4 and present), pre-deploy concurrency check (present, Task 9); section 6 (Task 11); section 7 phase 4 (Tasks 3, 5) and phase 5 (Tasks 8, 10, 11, 12). The spec's `Approval pending` custom metric has no task by decision 1.
- Type consistency: `mark_reminded` returns `ApprovalRecord | None` in Tasks 2 (store, notifier); `reminder_machine` is the name in `Workflow`, `Alarms` and the test helpers; `create_trail` values are the same three strings in Tasks 1 and 4; `Case` fields match the table and the tests in Task 5.
- Review Focus: lines 1 and 5 are tested in Task 3, 2 and 3 in Task 2, 4 in Task 4.
- Proportion: code blocks appear only where an exact condition expression, alarm shape or test assertion is pinned; construct bodies are left to the implementer.
