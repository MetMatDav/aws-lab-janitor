# Deployment runbook

How to deploy the lab janitor in dry-run mode and check that it works. In this
release (Plan 1) the stack only detects and reports; it never deletes anything.
Placeholders: `<account-id>` is the 12-digit AWS account id, `you@example.com`
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

## CloudTrail baseline

Check what CloudTrail already records before deploying, because Plan 3 adds
CloudTrail data events for the plans bucket and must not create a second trail
that duplicates management events.

Example, facts recorded on 2026-11-03 (UTC) for the owner's account:

- An AWS Organizations trail created by Control Tower
  (`aws-controltower-BaselineCloudTrail`) already covers the account. It is
  multi-region and logs management events to the organization's log-archive
  bucket.
- There are no CloudTrail Lake event data stores.
- Therefore Plan 3 will deploy with `createTrail=dataEventsOnly`.

To check another account, from the console open CloudTrail, then Trails and
Event data stores. From the command line:

```
python -c "import boto3; c = boto3.client('cloudtrail', region_name='us-east-1'); print(c.describe_trails(includeShadowTrails=True)['trailList']); print(c.list_event_data_stores()['EventDataStores'])"
```

`describe_trails` lists trails (look at `IsMultiRegionTrail` and
`IsOrganizationTrail`); `list_event_data_stores` lists CloudTrail Lake stores.

## Account quotas

On the owner's account the Lambda account concurrency quota ("Concurrent
executions") was 10, with 10 unreserved. Lambda rejects a reservation that
would leave the account with fewer than its minimum unreserved concurrency (100
on standard accounts), so with a quota of 10 no reservation is possible and the
deploy fails. Check yours in Service Quotas (AWS Lambda).

Until then, deploy with `-c reservedConcurrency=false`. Request a Service Quotas
increase of "Concurrent executions" to at least 101 (100 unreserved plus the
detector's 1), then redeploy without the flag so the detector gets its
reservation.

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

`--require-approval broadening` makes the CDK stop and ask before applying IAM
or security-group broadening, so a human reviews those changes on the first
deploy. Use `--require-approval never` only in non-interactive pipelines.

After the deploy:

1. Confirm the two SNS subscription emails (reports and alerts) once. Until
   you do, nothing is delivered to that address.
2. `dryRun` defaults to `true`, so the stack only reports.
3. Note the three stack outputs: `StateMachineArn`, `PlansBucketName` and
   `AuditTableName`.

   The stack outputs are currently named with the `Workflow` construct prefix
   (for example `WorkflowStateMachineArn...`). Use the value, not the key, in
   scripts.

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

For now the janitor's own plans bucket appears in the inventory. Excluding the
janitor's own resources is planned.

## Things you will see on first deploy

- The CDK CLI prints a notice about unconfigured feature flags. It is harmless.
- The schedule is ENABLED immediately, so the first scheduled run is the next
  Monday at 09:00 America/Bogota. If a previous janitor stack exists, it runs
  alongside this one until you retire it.
- Plan objects can only be deleted by the account root user. This is by
  design: the plans are audit evidence.
- Log groups keep 90 days of logs.
- The audit table and the plans bucket are retained on `cdk destroy`, so
  destroying the stack leaves them behind.

## Pause and resume

To pause the weekly run, redeploy with the schedule disabled:

```
npx cdk deploy LabJanitorStack --require-approval broadening \
  -c reportEmail=you@example.com -c alertEmail=you@example.com \
  -c reservedConcurrency=false -c janitorEnabled=false
```

To resume, redeploy without `-c janitorEnabled=false` (it defaults to `true`).

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

### Deploy fails on reserved concurrency

The account's Lambda concurrency quota is too low to reserve concurrency. See
[Account quotas](#account-quotas) and deploy with `-c reservedConcurrency=false`
until the quota is raised.
