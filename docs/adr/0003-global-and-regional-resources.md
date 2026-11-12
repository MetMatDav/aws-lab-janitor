# 0003. Global and regional resources are swept separately

## Status

Accepted on 2026-07-26, extended on 2026-11-11.

## Context

Most of what the janitor looks at lives in a region. S3 buckets, CloudFront
distributions and CloudWatch dashboards do not: asking for them in every region
would report each one 25 times. They still have an API endpoint somewhere, and
for dashboards that endpoint decides which region an IAM condition sees.

## Decision

Global services are swept once, by their own detector, and every finding and
error from them carries the region `global`. Regional detectors run once per
enabled region. When the executor has to act on a `global` item it uses a
client in `us-east-1`. A deployment that restricts the executor with
`allowedRegions` must therefore include `us-east-1`, and the synth refuses a
list without it.

## Consequences

- A bucket appears once in the report and once in the audit table.
- `global` is a word of this project, not an AWS region. It appears in audit
  keys (`RESOURCE#global#...`) and in emails, and has to be explained once.
- The `us-east-1` rule shuts out partitions where that region does not exist
  (GovCloud, China). They were never a target.
- Rejected: treating `us-east-1` as the home of global resources in the data.
  It would make a dashboard look like a regional resource of one region.

## Where to look

- [src/detector/detectors/global_.py](../../src/detector/detectors/global_.py):
  `REGION = "global"`.
- [src/executor/verify.py](../../src/executor/verify.py): `client_for` and
  `GLOBAL_CLIENT_REGION`.
- [lab_janitor/config.py](../../lab_janitor/config.py): `GLOBAL_SERVICES_REGION`.
- [tests/infra/test_config.py](../../tests/infra/test_config.py):
  `test_allowed_regions_must_include_us_east_1`.
