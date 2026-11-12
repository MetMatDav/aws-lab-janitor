# Deployment runbook

How to deploy the lab janitor in dry-run mode, approve a plan and check that it
works. As of Plan 2 the stack detects, reports, asks the owner for approval and
runs the executor, but deletions stay disabled until Plan 3: unless you deploy
with `-c dryRun=false`, every approved item is recorded as `SKIPPED` with the
reason `deletions-disabled`. Placeholders: `<account-id>` is the 12-digit AWS account id, `you@example.com`
is the mailbox that receives the reports and alerts.

## Prerequisites

- Python 3.13 managed by [uv](https://docs.astral.sh/uv/) and Node 22.
- Install the project environment (creates `.venv`, installs the Python
  requirements and runs `npm ci`):

  ```
  make install
  ```

- Sign in with AWS IAM Identity Center:

  ```
  aws sso login --sso-session <your-session>
  ```

- The identity you deploy with must be able to assume the CDK bootstrap roles
  (`cdk-*-deploy-role`, `cdk-*-file-publishing-role` and the like). An
  administrator or power-user permission set is enough.
- The account must have a `CDKToolkit` bootstrap stack. If it does not, run
  `npx cdk bootstrap` once for the account and region.

## CloudTrail

The plans bucket and the audit table are the janitor's evidence, and writes to
them are CloudTrail data events, which no trail records unless asked. The stack
can create a trail for them; `-c createTrail=<mode>` says how:

- `full` (the default): for an account with no trail. A multi-region trail with
  management events plus the two kinds of data events.
- `dataEventsOnly`: for an account whose management events already go to a
  trail (an organization trail, for example). Only the data events of
  `plans/` in the plans bucket and of the audit table, so nothing is recorded,
  or billed, twice.
- `none`: no trail and no log bucket.

Check what CloudTrail already records before the first deploy, so the stack
does not create a second trail that duplicates management events.

Example, facts recorded on 2026-11-03 (UTC) for the owner's account:

- An AWS Organizations trail created by Control Tower
  (`aws-controltower-BaselineCloudTrail`) already covers the account. It is
  multi-region and logs management events to the organization's log-archive
  bucket.
- There are no CloudTrail Lake event data stores.
- Therefore this account deploys with `-c createTrail=dataEventsOnly`.

To check another account, from the console open CloudTrail, then Trails and
Event data stores. From the command line:

```
python -c "import boto3; c = boto3.client('cloudtrail', region_name='us-east-1'); print(c.describe_trails(includeShadowTrails=True)['trailList']); print(c.list_event_data_stores()['EventDataStores'])"
```

`describe_trails` lists trails (look at `IsMultiRegionTrail` and
`IsOrganizationTrail`); `list_event_data_stores` lists CloudTrail Lake stores.

Cost: data events are billed per event (USD 0.10 per 100,000 at the time of
writing). A weekly run writes a few dozen items and two objects. In `full` mode
the first copy of management events in a region is free; a second trail that
also logs them is not, which is what `dataEventsOnly` avoids.

The trail delivers gzipped JSON files to the `TrailLogBucketName` output, under
`AWSLogs/<account-id>/CloudTrail/<region>/yyyy/mm/dd/`, usually within 5 to 15
minutes. The bucket keeps 400 days, accepts writes from this trail only and is
retained when the stack is deleted. To print the data events of one day that
mention a run id (no Athena needed):

```
python - <<'PY'
import boto3, gzip, json
bucket, account, day, run_id = "<TrailLogBucketName>", "<account-id>", "2026/11/11", "<runId>"
s3 = boto3.client("s3", region_name="us-east-1")
prefix = f"AWSLogs/{account}/CloudTrail/us-east-1/{day}/"
for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
    for obj in page.get("Contents", []):
        body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
        for record in json.loads(gzip.decompress(body))["Records"]:
            if run_id in json.dumps(record.get("requestParameters")):
                who = record["userIdentity"].get("arn", "").rsplit("/", 1)[-1]
                arns = [r.get("ARN") for r in record.get("resources", [])]
                print(record["eventTime"], record["eventName"], who, arns)
PY
```

Each line is one call that named the run: the detector's `PutObject` of the
plan and report and its `PutItem` calls, the notifier's and the executor's
`GetObject` of the plan, the executor's `PutItem` per outcome and the state
machine's mirror item. Reads are logged as well as writes. The `who` column is
the role session name, which for a Lambda function is the function name.

DynamoDB data events record the key of the item, the names of the attributes
written and the condition and update expressions, never an attribute value. A
conditional write that lost is recorded too, with `errorCode`
`ConditionalCheckFailedException`. The
calls on the approval record (`pk` `APPROVAL#<approvalId>`) therefore do not
match a run id; search for the approval id to see who created, claimed and
closed an approval. It also means the trail's log bucket holds approval ids in
clear, like the audit table does: keep read access to both equally narrow.

The deletions themselves (`DeleteSnapshot`, `DeleteVolume`, `ReleaseAddress`,
`DeleteDashboards`, `DeleteMonitor`) are management events. Event history keeps
90 days of them with no trail at all:

```
python -c "import boto3; c = boto3.client('cloudtrail', region_name='us-east-1'); [print(e['EventTime'], e['EventName'], [r.get('ResourceName') for r in e.get('Resources', [])]) for e in c.lookup_events(LookupAttributes=[{'AttributeKey': 'Username', 'AttributeValue': '<executor function name>'}])['Events']]"
```

Run it in the region where the resource lived. `DeleteDashboards` is recorded
in us-east-1.

## Account quotas

On the owner's account the Lambda account concurrency quota ("Concurrent
executions") was 10, with 10 unreserved. Lambda rejects a reservation that
would leave the account with fewer than its minimum unreserved concurrency (100
on standard accounts), so with a quota of 10 no reservation is possible and the
deploy fails. Check yours in Service Quotas (AWS Lambda).

Until then, deploy with `-c reservedConcurrency=false`. Request a Service Quotas
increase of "Concurrent executions" to at least 102 (100 unreserved plus 1
each for the detector and the executor), then redeploy without the flag so both
get their reservation.

## Deploy in dry-run

```
export AWS_PROFILE=<your-admin-profile> AWS_REGION=us-east-1 \
       CDK_DEFAULT_ACCOUNT=<account-id> CDK_DEFAULT_REGION=us-east-1 \
       PATH="$PWD/.venv/bin:$PATH"
npx cdk diff -c reportEmail=you@example.com -c alertEmail=you@example.com -c reservedConcurrency=false
npx cdk deploy LabJanitorStack --require-approval broadening \
  -c reportEmail=you@example.com -c alertEmail=you@example.com -c reservedConcurrency=false
```

Omit `-c reservedConcurrency=false` when your account quota allows reserved
concurrency; it is a safety nicety, not a requirement in this slice.

The CDK does not remember context between deploys: every `cdk deploy` must
repeat every `-c` flag of the one before, or the omitted settings fall back to
their defaults. Leaving out `-c createTrail=dataEventsOnly` turns the trail
into a multi-region trail with management events; leaving out `-c dryRun=false`
after the cutover disarms the executor. The examples in this runbook show only
the flags they are about. To stop typing them, put your values in
`cdk.context.json` next to `cdk.json` (it is git-ignored, so the addresses
stay out of the repository) and deploy with no `-c` at all:

```
{
  "reportEmail": "you@example.com",
  "alertEmail": "you@example.com",
  "createTrail": "dataEventsOnly",
  "reservedConcurrency": "false"
}
```

Run `npx cdk diff` before a deploy: a trail, an alarm or the executor's
`DELETIONS_ENABLED` changing when you did not mean to is a forgotten flag.

With the flag, the executor also loses its reserved concurrency of 1, so a
retried `Apply` could run two executors on the same plan. Both re-verify every
item and the audit writes are conditional, so nothing is deleted twice or
outside the plan, but the counts in the result email could overlap. Remove the
flag after the quota increase in Plan 3.

Deploy with the default CDK bootstrap roles. Do not pass `cdk deploy
--role-arn <other role>`: the approvals topic policy allows `sns:Subscribe` and
topic-attribute changes only to the bootstrap's CloudFormation execution role
and the account root, so a deploy run through any other role is refused on that
topic.

Approval settings are context values too (all optional):

- `approvalEmail`: mailbox for approval requests; defaults to `reportEmail`.
- `approvalAuth`: `link` (default), `iam` or `both`; see [Approval flow](#approval-flow).
- `approverRoleArn`: IAM role ARN allowed to decide through the IAM route;
  required by `iam` and `both`.
- `createTrail`: `full` (default), `dataEventsOnly` or `none`; see
  [CloudTrail](#cloudtrail).
- `allowedRegions`: comma-separated regions the executor may delete in; empty
  means every region. When set it must include `us-east-1`, because the
  executor calls the CloudWatch dashboard API through us-east-1; without it
  dashboard deletions fail with `AccessDenied` and are recorded as `FAILED`,
  nothing else happens.
- `dryRun`: defaults to `true`. Only `-c dryRun=false` arms the executor
  (`DELETIONS_ENABLED=true`) and makes the Monday run ask for approval.

`--require-approval broadening` makes the CDK stop and ask before applying IAM
or security-group broadening, so a human reviews those changes on the first
deploy. Use `--require-approval never` only in non-interactive pipelines.

After the deploy:

1. Confirm the three SNS subscription emails (reports, alerts and approvals) once. Until
   you do, nothing is delivered to that address. SNS does not count a publish
   to a topic with no confirmed subscriber as a failure, so the notifier checks
   first: a run that would email an unconfirmed reports or approvals topic
   fails with `NoConfirmedSubscriberError` and a `[Lab Janitor] Run failed`
   email. Nothing guards the alerts topic itself, so confirm that one first.
   Check before the first manual run:

   ```bash
   aws sns list-subscriptions --query "Subscriptions[?contains(TopicArn, 'LabJanitorStack')].[SubscriptionArn,Endpoint]" --output table
   ```

   A row whose first column reads `PendingConfirmation` has not been confirmed.
   The confirmation email comes from `no-reply@sns.amazonaws.com` with the
   subject `AWS Notification - Subscription Confirmation`; look in spam. The
   link expires after three days. To send it again, open the subscription in
   the SNS console and choose **Request confirmation**, or call `aws sns
   subscribe` again with the same topic, protocol and endpoint (reports and
   alerts topics only; for the approvals topic see
   [Things you will see on first deploy](#things-you-will-see-on-first-deploy)).
2. `dryRun` defaults to `true`, so the stack only reports.
3. Note the stack outputs: `StateMachineArn`, `PlansBucketName`,
   `AuditTableName`, `ApprovalApiUrl` (the base URL of the approval API,
   `https://<api-id>.execute-api.<region>.amazonaws.com`) and, unless
   `createTrail` is `none`, `TrailLogBucketName`.

## Manual dry-run

Do not wait for Monday. Start an execution with this input, from the Step
Functions console (Start execution) or with boto3:

```
{"dryRun": true, "minAgeDays": 7}
```

```
python -c "import boto3; print(boto3.client('stepfunctions', region_name='us-east-1').start_execution(stateMachineArn='<StateMachineArn>', input='{\"dryRun\": true, \"minAgeDays\": 7}')['executionArn'])"
```

The boto3 snippets in this section and the local detector run below need the
project venv (`PATH="$PWD/.venv/bin:$PATH"` or an activated venv) and
`AWS_REGION=us-east-1`, set in the same shell as the Deploy section.

Wait for the execution to reach `SUCCEEDED`, then check:

- The report email. Its subject starts with `[Lab Janitor] Weekly report` and
  ends with `(DRY RUN)`.
- In the plans bucket (`PlansBucketName`): `plans/<runId>/plan.json` and
  `plans/<runId>/report.txt`.
- In the audit table (`AuditTableName`): the items whose partition key is
  `RUN#<runId>`.

Reading the report:

- The ERRORS section lists `service-unavailable-in-region` for services that
  do not exist in opt-in regions. This is expected.
- `ACCOUNT CLEAN` never appears while errors exist, because a region with an
  error was not fully seen.

To run the detector locally against your current credentials (no upload, no
audit writes), export the markers the Lambda would receive and run the module.
The values below are the ones in `lab_janitor/markers.py`; `MIN_AGE_DAYS` is
optional and defaults to 7:

```
export LAB_NAME_PREFIX=LAB- LAB_TAG_KEY=Purpose LAB_TAG_VALUE=lab \
       KEEP_TAG_KEY=janitor KEEP_TAG_VALUE=keep MIN_AGE_DAYS=7
PYTHONPATH=src python -m detector.handler
```

It prints the dry-run plan as JSON.

## First dry-run: what a healthy run looks like

Shape observed on the first dry-run, numbers only:

- State path: `Defaults`, `Normalize`, `Detect`, `HasCandidates`,
  `NotifyReportOnly`.
- Duration: about 25 seconds for 25 regions.
- The report INVENTORY lists the existing buckets, distributions and
  dashboards.
- The ERRORS section lists `internetmonitor:ListMonitors
  service-unavailable-in-region` for the opt-in regions where the service does
  not exist. This is expected and harmless.
- No `ACCOUNT CLEAN` line appears while errors exist.
- The plans bucket holds `plans/<runId>/plan.json` and
  `plans/<runId>/report.txt`.
- The audit table holds `RUN#<runId>` items: one `DETECTED` per finding and
  one `SUMMARY`.

The janitor's own resources (tag `Project=aws-lab-janitor`) are left out of the
inventory.

## Things you will see on first deploy

- The CDK CLI prints a notice about unconfigured feature flags. It is harmless.
- A third subscription confirmation email arrives for the approvals topic,
  next to the reports and alerts ones. Until you confirm it, a run with
  candidates fails in `RequestApproval` before it writes or sends anything. The new
  `ApprovalApiUrl` output is the base URL of the approval API.
  Resending the confirmation with `aws sns subscribe` does not work for the
  approvals topic: its policy denies `sns:Subscribe` to every principal except
  the CDK CloudFormation execution role and the account root. If the 3-day
  link lapses, redeploy the stack or use a root session.
- A run that is waiting for approval shows as `RUNNING` in Step Functions for
  up to 72 hours. The state machine execution timeout is 74 hours (the 72 hour
  approval window plus 2 hours for the rest of the run).
- The schedule is ENABLED immediately, so the first scheduled run is the next
  Monday at 09:00 America/Bogota. If a previous janitor stack exists, it runs
  alongside this one until you retire it.
- Plan objects can only be deleted by the account root user. This is by
  design: the plans are audit evidence.
- Log groups keep 90 days of logs.
- The audit table and the plans bucket are retained on `cdk destroy`, so
  destroying the stack leaves them behind.

## Approval flow

A run asks for approval only when it is not a dry run (`"dryRun": false` in the
execution input, or a stack deployed with `-c dryRun=false`) and the detector
found at least one candidate. The state path is `Defaults`, `Normalize`,
`Detect`, `HasCandidates`, `NewApprovalId`, `StartReminder`, `RequestApproval`
(waits for the decision), `Apply`, `RecordApplied`, `NotifyResult`. A rejection
goes through
`RecordRejected`, `MirrorRejected`, `NotifyRejected`; an expiry through
`RecordExpired`, `MirrorExpired`, `NotifyExpired`. Dry runs end at
`NotifyReportOnly` with only the weekly report.

The approval email (subject `[Lab Janitor] Approval needed: ...`) always has
the candidate count, the estimated monthly cost, the expiry time, the plan
sha256, the plan summary and the S3 location of the full plan. What it says
about deciding depends on `approvalAuth`:

- `link`: a review link `<ApprovalApiUrl>/approvals/<approvalId>` and the
  4-character confirmation code (the last four characters of the run id,
  case-insensitive). Open the link, read the plan, type the code and press
  approve or reject. Anyone holding the email can decide, so keep it private.
- `iam`: no link; the two commands `python scripts/approve.py --url
  <ApprovalApiUrl> <approvalId> approve` and `... reject`. See
  [IAM approval](#iam-approval).
- `both`: both blocks; either works until the first decision.

The request is valid for about 72 hours (the record expires 30 minutes before
the 72 hour task timeout). After that nothing is deleted for that run.

Sixty hours after the request, if the approval is still `PENDING`, a reminder
(subject `[Lab Janitor] Reminder: approval pending, run <runId>`) goes to the
approvals topic with the same link or commands. It is sent at most once: the
approval record gets a `remindedAt` timestamp, and a decided or expired
approval is never reminded. The reminder is a second state machine
(`Wait`, `Remind`, `Reminded`) that `StartReminder` starts without waiting for
it, so every run that asks for approval leaves one reminder execution
`RUNNING` for 60 hours; it ends without sending anything when the approval was
already decided. If the reminder cannot be started, the run still asks for
approval. To see a reminder without waiting, start an execution of the
reminder state machine by hand with
`{"approvalId": "<approvalId>", "runId": "<runId>", "waitSeconds": 5}` while an
approval is pending.

A retried approval request (a Lambda retry) refreshes the stored task token and
sends the approval email again, so the owner may receive the same request
twice. Either copy works until the first decision.

What the answers mean (the link form shows an HTML page, the IAM route returns
JSON):

| Status | Meaning |
| ------ | ------- |
| `200` | The decision was recorded and the run resumed. |
| `400` | Incomplete or malformed form or body. On the link route also a wrong confirmation code: the code is checked before anything is written, so the record stays `PENDING` and you can retry. |
| `403` | IAM route only: the caller is not the configured approver. |
| `404` | No such approval, or the route is not enabled by `approvalAuth`. |
| `409` | Already decided. A second decision never changes the first. |
| `410` | The approval expired (after the approval window: 71 h 30 min; the state machine waits 72 h). Nothing will be deleted for this run. |
| `500` | The decision was not recorded: Step Functions refused the callback and the claim was put back to `PENDING`. Retry in a few minutes. |
| `504` | Step Functions did not answer, so the decision may or may not have been delivered. The record stays `DECIDING` and every later attempt gets `409`. Look for the result or rejection email; if the run is still waiting after a few minutes, see [A decision left in DECIDING](#a-decision-left-in-deciding). |

To follow one approval in the audit table (`AuditTableName`):

- `pk = APPROVAL#<approvalId>`, `sk = META`: the approval record. `status`
  moves `PENDING`, `DECIDING`, then `APPROVED`, `REJECTED` or `EXPIRED`;
  `approvedBy` says who decided (link: source IP and user agent; IAM: the
  caller ARN).
- `pk = RUN#<runId>`, `sk = APPROVAL#<approvalId>`: the mirror under the run,
  with event `APPROVED`, `REJECTED` or `EXPIRED`, written by the state machine
  (actor `statemachine`).
- `pk = RUN#<runId>`, `sk = RESOURCE#<region>#<kind>#<id>#DELETED`, `#FAILED`
  or `#SKIPPED`: one executor outcome per approved item (actor
  `lambda:executor`). Each item has exactly one outcome.

```
python -c "import boto3; t = boto3.resource('dynamodb', region_name='us-east-1').Table('<AuditTableName>'); print(t.get_item(Key={'pk': 'APPROVAL#<approvalId>', 'sk': 'META'}).get('Item'))"
```

### A decision left in DECIDING

A `504` answer, or a Lambda that died between the callback and the final write,
leaves the approval record in `DECIDING`. Nothing decides it again by itself,
on purpose: a second callback racing an unknown first one would be worse than
a manual step.

First find out whether the decision arrived. Open the execution in Step
Functions and look at `RequestApproval`:

- It ended and the run went on to reject (`RecordRejected`): the decision was
  delivered and the state machine closed the record itself. Nothing to do.
- It ended and the run went on to `Apply`: the approval was delivered, but the
  executor applies only a record that reads `APPROVED`. It waits a few seconds
  for the record, then fails with `approval is not APPROVED`, the run ends
  `FAILED` with an alert, and nothing is deleted. Do not edit the record: start
  a new run and approve its plan.
- It is still running: the callback was lost. Put the record back to `PENDING`
  from an administrator session and decide again from the email:

```
python -c "import boto3; boto3.client('dynamodb', region_name='us-east-1').update_item(TableName='<AuditTableName>', Key={'pk': {'S': 'APPROVAL#<approvalId>'}, 'sk': {'S': 'META'}}, UpdateExpression='SET #s = :pending REMOVE approvedBy', ConditionExpression='#s = :deciding', ExpressionAttributeNames={'#s': 'status'}, ExpressionAttributeValues={':pending': {'S': 'PENDING'}, ':deciding': {'S': 'DECIDING'}})"
```

The condition makes the reset fail if the record is no longer `DECIDING`. If
the approval window closes first, the state machine marks it `EXPIRED` and the
run ends without deleting anything.

## IAM approval

`approvalAuth=iam` (or `both`) exposes `POST /iam/approvals/{id}`, signed with
SigV4. The handler accepts only the role set in `approverRoleArn` (a session of
that role is fine, including IAM Identity Center roles under
`role/aws-reserved/sso.amazonaws.com/...`); any other caller gets `403`. The
caller's identity policy must also allow `execute-api:Invoke` on the API, which
an administrator permission set already does.

Sign in and run the CLI from the project venv with the profile of that role:

```
aws sso login --sso-session <your-session>
.venv/bin/python scripts/approve.py --url <ApprovalApiUrl> <approvalId> approve \
  --profile <approver-profile> --region us-east-1
```

Use `reject` instead of `approve` to refuse. `--profile` defaults to your
environment's; the request is signed for the region in the API URL unless
`--region` says otherwise. The script prints `status: <code>` and the JSON answer
(`{"status": "APPROVED"}` or `{"status": "REJECTED"}`), exits 0 on a 2xx
answer and 1 otherwise, and refuses a URL that is not `https://`.

## Dress rehearsal with deletions disabled

Use this to test the whole approval path without risking anything: with the
default `dryRun=true` the executor is disarmed (`DELETIONS_ENABLED=false`), so
an approved plan ends with every item `SKIPPED` and reason `deletions-disabled`,
even though the execution itself is not a dry run.

1. Deploy as in [Deploy in dry-run](#deploy-in-dry-run) (no `-c dryRun=false`)
   and confirm all three subscriptions.
2. Create a decoy that the janitor will propose, for example an empty
   CloudWatch dashboard with the lab prefix (`LAB-`). Dashboards are proposed
   by name, with no age threshold:

   ```
   aws cloudwatch put-dashboard --dashboard-name LAB-decoy --dashboard-body '{"widgets":[]}'
   ```

3. Start a live execution with `{"dryRun": false, "minAgeDays": 7}` (see
   [Manual dry-run](#manual-dry-run) for the command). It waits in
   `RequestApproval`. The approval email arrives with the decoy as the
   candidate, the report email arrives without the `(DRY RUN)` marker, and
   `APPROVAL#<approvalId>` / `META` has `status` `PENDING`.
4. With the link: type a wrong code first and expect `400` with the record still
   `PENDING`; then the right code and approve. With IAM: run the CLI.
5. The execution continues through `Apply` and ends `SUCCEEDED`. The result email
   reads `0 deleted, 0 failed, 1 skipped`. The audit table holds
   `RESOURCE#global#cw-dashboard#LAB-decoy#SKIPPED` under the run and the mirror
   `APPROVAL#<approvalId>` with event `APPROVED`. A second decision on the same
   approval returns `409`.
6. Delete the decoy yourself:

   ```
   aws cloudwatch delete-dashboards --dashboard-names LAB-decoy
   ```

To rehearse a rejection or an expiry, decide `reject`, or wait out the 72 hours,
and look for the `REJECTED` or `EXPIRED` mirror and the matching email.

### Rehearsal record: 2026-11-11

Run `20261111T192012Z-ecb0`, approval `7f69146f-7354-4595-bf89-bf59b304e762`,
decoy `LAB-decoy-plan2`, decided through the email link.

- State path: `Defaults`, `Normalize`, `Detect`, `HasCandidates`,
  `NewApprovalId`, `RequestApproval`, `Apply`, `RecordApplied`, `NotifyResult`,
  `Applied`. About 25 seconds from start to the approval wait, 2 seconds from
  the decision to `SUCCEEDED`.
- The plan held one candidate (`cw-dashboard LAB-decoy-plan2`, reason `name
  prefix LAB-`). The approval record went `PENDING` to `APPROVED`.
- The executor logged `armed=False` and `0 deleted, 0 failed, 1 skipped`. The
  audit table holds `DETECTED`, `PROPOSED` and `SKIPPED` (`deletions-disabled`)
  for the decoy, and the `APPROVED` mirror with the counts. The decoy was still
  there after the run and was deleted by hand.
- Delivery, not just publish: the reports topic delivered 2 of 2 messages
  (weekly report and result) and the approvals topic delivered the request,
  with no failed notifications.
- Once decided, both `GET` and `POST` on the approval returned `409` and the
  record did not change. A form with an invalid decision returned `400`.

Not exercised in this rehearsal: a wrong confirmation code on a `PENDING`
approval, the reject path, the expiry path and the IAM route. They are covered
by unit tests only; see the follow-ups file.

### Operational rehearsal record: 2026-11-11 and 2026-11-12

Plan 3 template, deployed on 2026-11-11 with `createTrail=dataEventsOnly` and
deletions disabled. Decoy `LAB-decoy-plan3`, deleted by hand afterwards.

- After the deploy: six alarms, five of them `OK` within three minutes; the
  heartbeat left `INSUFFICIENT_DATA` 42 minutes later. The trail was logging
  with the two data selectors and no management selector.
- Reminder (run `20261111T203848Z-138c`): `StartReminder` started a reminder
  execution with `waitSeconds` 216000. A reminder started by hand with
  `waitSeconds` 5 sent the email and stamped `remindedAt`; a second one returned
  `{"reminded": false}` and sent nothing. The 60 hour execution was stopped by
  hand. That approval was never opened and is left to expire on 2026-11-14 to
  see the expiry path.
- Failure path: an execution with input `[]` failed in `Defaults` with
  `States.ReferencePathConflict` and never reached `NotifyFailure`.
  `ExecutionFailed` went to `ALARM` 17 seconds after the start and back to `OK`
  15 minutes later; both emails arrived. History in
  `docs/evidence/alarm-history-2026-11-12.json`.
- Wrong code, then reject (run `20261112T173931Z-169b`, approval
  `e03cffb7-a9da-45ce-9685-7f96da7ba810`, email link). Access log: `GET 200`,
  `POST 400` (handler log `confirmation code mismatch, decision refused`, record
  still `PENDING`), `GET 200`, `POST 200`, and `GET 409` when the page was
  reloaded after the decision.
- State path after the decision: `RecordRejected`, `MirrorRejected`,
  `NotifyRejected`, `Rejected`; `SUCCEEDED` one second after the decision. The
  record is `REJECTED`, the mirror event under the run is `REJECTED`, and the
  executor had zero invocations on both days. The reports topic delivered the
  weekly report and the rejection email, the approvals topic delivered the
  request, with no failed notifications.
- The handler and the state machine race to close the record, as designed. The
  handler won: `RecordRejected` got `ConditionalCheckFailedException`, the catch
  moved on, and the handler logged no error.
- Data events reached the trail bucket about five minutes after the calls. For
  the rejected run they show, in order: the detector's two `PutObject` under
  `plans/<runId>/` and its seven `PutItem`, the notifier's two `GetObject` and
  its `PutItem` of the approval record, the handler's `GetItem` per request and
  its two `UpdateItem` (claim, then close), the state machine's failed
  `UpdateItem` and its `PutItem` of the mirror. The operator's own reads of the
  table during the rehearsal are there as well.
- Defect: `ReportsUndelivered` fired at 17:42:00 UTC and recovered at 17:43:00,
  with both emails delivered. `ApprovalsUndelivered` did not change state.
  Both alarms were removed after this rehearsal; see [Alarms](#alarms).

Not exercised: the expiry path (pending, see above), the IAM route, and a
`POST` on a decided approval (seen on 2026-11-11).

## Pause and resume

To pause the weekly run, redeploy with the schedule disabled:

```
npx cdk deploy LabJanitorStack --require-approval broadening \
  -c reportEmail=you@example.com -c alertEmail=you@example.com \
  -c reservedConcurrency=false -c janitorEnabled=false
```

To resume, redeploy without `-c janitorEnabled=false` (it defaults to `true`).
Both commands need the rest of your flags too (`createTrail`, `dryRun`; see
[Deploy in dry-run](#deploy-in-dry-run)).

After resuming, start a manual run and check that the heartbeat alarm is `OK`.
If the pause lasted more than seven days the alarm is already in `ALARM`, and
CloudWatch does not notify again for an alarm that was in that state when its
actions were switched back on: a schedule that failed to resume would tell
nobody.

## IAM simulation

`tests/iam/simulate_policies.py` asks IAM what the executor role's policy
decides for 22 fixed cases: lab and non-lab names, the lab tag present and
absent, the keep tag, and actions the janitor must never have
(`TerminateInstances`, `DeleteBucket`, `CreateTags` and others). The last run is
committed as `docs/evidence/iam-simulation.txt`. Rerun it after any change to
the executor's policy, from the repository root:

```
python -m tests.iam.simulate_policies --profile <any-profile-with-iam-read>
```

It needs credentials for one read-only call (`iam:SimulateCustomPolicy`), exits
1 if a decision differs from the expected one, and synthesizes the stack itself
for a placeholder account, so the report never carries a real account id. Do
not point it at `cdk.out`: the CDK CLI replaces `CDK_DEFAULT_ACCOUNT` with the
caller's account.

What it proves is the policy logic: for a resource with these tags, this is the
decision. That each service sends the tag key when it authorizes the call comes
from the Service Authorization Reference, which lists `aws:ResourceTag` for the
EC2 snapshot, volume and Elastic IP resource types, the CloudWatch dashboard
and the Internet Monitor monitor (checked on 2026-11-11). The keep-tag Deny has
not been exercised against a live call: the executor re-verifies each item and
skips a kept resource before IAM is ever asked.

## Alarms

Every alarm notifies the alerts topic when it fires and again when it recovers.
Confirm the alerts subscription first: nothing can alarm about the alerts topic
itself.

| Alarm | It means | What to do |
| ----- | -------- | ---------- |
| `DlqNotEmpty` | The scheduler could not start a run after its retries, or SNS could not deliver an email. The message is in the dead-letter queue. | Read the message (below) to see which. For a failed start, check the state machine and the schedule, then start a run by hand. For a failed email, check the address. Purge the queue when done; the alarm clears. |
| `ExecutionFailed` | A weekly execution failed or timed out. Most failures also send a `[Lab Janitor] Run failed` email from the `NotifyFailure` state, so expect two emails; a failure before or inside that state sends only this one. | Open the execution in Step Functions and read the failed state. Nothing is deleted by a failed run unless the result email says so. Start a new run once fixed. |
| `ReminderFailed` | An execution of the reminder state machine failed. The approval itself is unaffected. | Decide the pending approval from the original email. Read the failed execution for the cause. |
| `Heartbeat` | No execution started in the last 7 days: the schedule is disabled or broken, or the stack is. | Check the schedule state and the last execution. Start a manual dry-run; the alarm recovers within one to two hours (alarms over daily periods are evaluated hourly). |

Things to know:

- On a brand new stack the heartbeat alarm fires once, because seven days
  without data are seven days without a run. Start a manual dry-run right after
  deploying and it recovers.
- With `-c janitorEnabled=false` the heartbeat alarm still changes state but
  its actions are disabled, so a deliberate pause sends nothing.
- The heartbeat can raise a false alarm for up to an hour if a Monday run starts
  later than the one of the week before. The recovery notification follows.
- No alarm watches email delivery. The notifier lists the subscriptions of a
  topic before publishing and refuses when none is confirmed, which fails the
  run; see [Troubleshooting](#a-run-failed-with-noconfirmedsubscribererror).
  Two alarms that compared the SNS publish and delivery metrics existed for one
  day and were removed: CloudWatch can receive the publish datapoint of a
  message before its delivery datapoint, so they fired on healthy runs
  (2026-11-12, `docs/evidence/alarm-history-2026-11-12.json`).
- The alarms moved to their own construct in Plan 3, so their names changed
  once on that deploy.

Reading and purging the dead-letter queue (the queue name is in the SQS console
and starts with `LabJanitorStack-Dlq`):

```
python -c "import boto3; q = boto3.client('sqs', region_name='us-east-1'); url = q.get_queue_url(QueueName='<dlq-name>')['QueueUrl']; [print(m['Body']) for m in q.receive_message(QueueUrl=url, MaxNumberOfMessages=10, VisibilityTimeout=0).get('Messages', [])]"
python -c "import boto3; q = boto3.client('sqs', region_name='us-east-1'); q.purge_queue(QueueUrl=q.get_queue_url(QueueName='<dlq-name>')['QueueUrl'])"
```

## Troubleshooting

### Deploy fails with "SSM parameter /cdk-bootstrap/<qualifier>/version not found"

Seen on the first deployment (2026-11-03 (UTC)). The `CDKToolkit` stack existed
(default qualifier `hnb659fds`, bootstrap version 32), but the SSM parameter
had been deleted outside CloudFormation. `cdk bootstrap` did not recreate it
because the bootstrap template was unchanged, so there was no change set.
CloudFormation drift detection on the logical resource `CdkBootstrapVersion`
reported `DELETED`.

Diagnose with boto3:

```
python - <<'EOF'
import boto3
cfn = boto3.client("cloudformation", region_name="us-east-1")
ssm = boto3.client("ssm", region_name="us-east-1")

stack = cfn.describe_stacks(StackName="CDKToolkit")["Stacks"][0]
print([p for p in stack["Parameters"] if p["ParameterKey"] == "Qualifier"])
print([o for o in stack["Outputs"] if o["OutputKey"] == "BootstrapVersion"])

print(ssm.describe_parameters(
    ParameterFilters=[{"Key": "Name", "Option": "BeginsWith", "Values": ["/cdk-bootstrap/"]}]
)["Parameters"])

drift = cfn.detect_stack_resource_drift(
    StackName="CDKToolkit", LogicalResourceId="CdkBootstrapVersion"
)
print(drift["StackResourceDrift"]["StackResourceDriftStatus"])
EOF
```

If the stack output shows the version (32 here), no SSM parameter is listed
under `/cdk-bootstrap/`, and drift reports `DELETED`, the parameter was removed
outside CloudFormation.

Fix: recreate the parameter with the version CloudFormation expects, then
deploy again:

```
aws ssm put-parameter --name /cdk-bootstrap/<qualifier>/version --type String \
  --value <BootstrapVersion-output-of-CDKToolkit>
```

In this incident the qualifier was `hnb659fds` and the version was 32.

A later `cdk bootstrap` that changes the template will update the parameter
normally.

### A run failed with `NoConfirmedSubscriberError`

The notifier refused to publish because the topic named in the error has no
confirmed subscription: it is still `PendingConfirmation`, or someone used the
unsubscribe link at the bottom of an email. The check runs before the state
does anything else, so `NotifyReportOnly` sent no report and `RequestApproval`
wrote no approval record. In `NotifyResult`, `NotifyRejected` or
`NotifyExpired` the outcome is already in the audit table and only the email is
missing; for a result, read the items under `RUN#<runId>`. A reminder that hits
this fails its own execution and raises `ReminderFailed`.

Confirm the subscription as in [Deploy in dry-run](#deploy-in-dry-run), then
start a new run.

### A run succeeded but no report email arrived

A confirmed subscription existed when the notifier published, and it logged
`weekly report published (messageId=...)`. Look in spam first. Then compare
`NumberOfNotificationsDelivered` and `NumberOfNotificationsFailed` of the topic
for that minute: a failed delivery is redriven to the dead-letter queue and
raises `DlqNotEmpty`.

### Deploy fails on reserved concurrency

The account's Lambda concurrency quota is too low to reserve concurrency. See
[Account quotas](#account-quotas) and deploy with `-c reservedConcurrency=false`
until the quota is raised.

### Deploy fails with "Invalid parameter: Email address" on an SNS subscription

The deploy command was run with a placeholder (for example `-c
reportEmail=you@example.com` copied verbatim, or an unexpanded shell variable)
instead of a real address. SNS validates the endpoint only when CloudFormation
creates or replaces the subscription, so every other change in that update is
cancelled and the stack rolls back to `UPDATE_ROLLBACK_COMPLETE`. Nothing is
lost: the previous subscriptions and functions stay as they were, and the stack
can be deployed again as soon as it reaches that status.

Since Plan 2, `cdk synth` refuses an email that is not shaped like
`local@domain.tld` ("reportEmail must be an email address such as
you@example.com"), so the mistake now stops before CloudFormation. Fix the
command and deploy again.
