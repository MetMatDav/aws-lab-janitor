# aws-lab-janitor

A weekly serverless janitor that finds forgotten resources in an AWS lab account
and reports them. This first release (Plan 1) only detects and never deletes
anything: an EventBridge Scheduler schedule starts a Step Functions workflow
every Monday, a detector Lambda sweeps every enabled region plus the global
services in dry-run mode, writes an immutable plan and its report to a versioned
S3 bucket, records each finding in a DynamoDB audit table, and a notifier Lambda
emails the weekly report through SNS. Approval and deletion come in later plans.

Work in progress; see docs/superpowers/specs.

## Toolchain

aws-cdk CLI 2.1147.0 or later is required by aws-cdk-lib 2.275.0 (cloud assembly schema 55).

## Configuration

Settings are CDK context values, passed with `-c key=value` or set in `cdk.json`.
`reportEmail` is required (or set `JANITOR_REPORT_EMAIL`).

- `reservedConcurrency=false`: do not reserve Lambda concurrency for the detector.
  Use it in accounts with a small Lambda concurrency quota (new accounts default
  to 10), where Lambda refuses to reserve concurrency and the deploy fails, until
  a quota increase lands.

## Deploy (dry-run)

Full steps, prerequisites and verification are in [docs/runbook.md](docs/runbook.md).
In short:

```
export AWS_PROFILE=<your-admin-profile> AWS_REGION=us-east-1 \
       CDK_DEFAULT_ACCOUNT=<account-id> CDK_DEFAULT_REGION=us-east-1 \
       PATH="$PWD/.venv/bin:$PATH"
npx cdk diff -c reportEmail=you@example.com -c alertEmail=you@example.com
npx cdk deploy LabJanitorStack --require-approval never \
  -c reportEmail=you@example.com -c alertEmail=you@example.com
```

Add `-c reservedConcurrency=false` in accounts with a small Lambda concurrency quota.
`dryRun` defaults to true, so the stack only reports.
