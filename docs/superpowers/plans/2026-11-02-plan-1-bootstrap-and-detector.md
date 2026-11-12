# Lab Janitor Plan 1: Bootstrap and Detector (dry-run pipeline) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the new `aws-lab-janitor` repository and deliver a deployable dry-run janitor: a corrected detector that writes an immutable plan and report to S3, records audit events, and emails the report through a Step Functions workflow.

**Architecture:** One Lambda asset directory `src/` shared by all functions (`shared/` library plus one package per function). CDK constructs in `lab_janitor/` wire a Standard state machine (Normalize, Detect, HasCandidates, NotifyReportOnly, NotifyFailure) behind an EventBridge Scheduler. The approval branch, the executor and the IAM split for deletions arrive in Plan 2; this plan never deletes anything and always ends in the report-only path.

**Tech Stack:** Python 3.13, boto3 (runtime), AWS CDK v2 (aws-cdk-lib pinned), pytest + botocore Stubber, ruff, cdk-nag, cfn-lint, GitHub Actions, uv for the local interpreter.

**Spec:** `docs/superpowers/specs/2026-11-02-lab-janitor-portfolio-design.md` (copied into the new repo in Task 1). Sections implemented here: 3, 4.1 (reports and alerts topics), 4.2 (states Defaults, Normalize, Detect, HasCandidates, ApprovalNotYetImplemented, NotifyReportOnly, NotifyFailure), 4.4, 4.5 (run-event family only), 4.7 (detector, notifier report mode, state machine, scheduler), 4.8, 4.10 (DLQ and execution-failed alarms), 5.1 to 5.4; phases 0, 1 and 2 of section 7 plus the report-only slice of phase 3 and the two alarms of phase 4. Where this plan and spec 4.4 differ (run id format `20261110T140012Z-7f3a`, candidate key `resourceId`), the plan wins; Plan 4 updates the spec example.

**Plan series:** Plan 1 (this) bootstrap and detector; Plan 2 approval flow and executor; Plan 3 operations, CloudTrail and cutover with the controlled deletion test; Plan 4 story (README, ADRs, threat model, evidence, publication).

## Global Constraints

- Everything in the repo in English: code, comments, docstrings, commit messages, report text, resource names.
- Python 3.13 everywhere: `.python-version`, `requires-python = ">=3.13,<3.14"`, CI, Lambda `Runtime.PYTHON_3_13`.
- No em dashes in AWS resource names or descriptions; hyphens only.
- No account id, email address or secret in any file or commit. Account and region come from `CDK_DEFAULT_ACCOUNT` / `CDK_DEFAULT_REGION`; emails from cdk context.
- `cdk synth` and `pytest` must pass with no AWS credentials.
- Default `dryRun` is `true` at every layer; only a boolean `false` disables it.
- Marker constants defined once in `lab_janitor/markers.py`: `LAB_NAME_PREFIX = "LAB-"`, `LAB_TAG_KEY = "Purpose"`, `LAB_TAG_VALUE = "lab"`, `KEEP_TAG_KEY = "janitor"`, `KEEP_TAG_VALUE = "keep"`. The Lambda side reads them from environment variables only.
- Minimum age default 7 days; report email truncated at 150 000 bytes; SNS subjects under 100 characters.
- Placeholders allowed in tests, CI and snapshots: `example.com` addresses and account id `123456789012`.
- cdk-nag 3.x API (pinned 3.0.2, needs aws-cdk-lib >= 2.257.0): packs are registered with `aws_cdk.Validations.of(app).add_plugins(AwsSolutionsChecks(app, verbose=True), ServerlessChecks(app))`; each finding is acknowledged with `aws_cdk.Validations.of(<construct>).acknowledge(aws_cdk.Acknowledgment(id="<RuleId>[<FindingId>]", reason="..."))`, one exact id per call (no prefix matching). `NagSuppressions` does not exist in 3.x. Packs are registered in Task 13 only, so earlier tasks synthesize without validation errors.
- Python 3.13 stays even though aws-cdk-lib 2.275.0 exposes `PYTHON_3_14`: `AwsSolutions-L1` and `Serverless-LambdaLatestVersion` are acknowledged with the reason `Python 3.13 pinned across repo, CI and runtime; upgrade tracked in roadmap`.
- New repo path: `<repo>`. The old repo at `<old-repo>` is read-only reference; never commit there from this plan.

## Review Focus

1. A region where a service does not exist (Internet Monitor in opt-in regions): the detector must record `service-unavailable-in-region` and keep sweeping. Test in Task 4.
2. An execution input of `{"config": {"dryRun": "false"}}` (string) or no `config` at all: must stay dry-run. Tests in Task 2 (settings) and Task 13 (state machine Choice).
3. Thousands of findings: the email must stay under the SNS limit and point to S3. Test in Task 10.
4. A lab-tagged snapshot 2 days old, or one that backs an AMI, or one tagged `janitor=keep`: reported, never a candidate. Tests in Task 4.
5. Lambda time budget exhausted mid-sweep: remaining regions appear as `time-budget` errors and the plan is still written. Test in Task 9.

---

### Task 1: Bootstrap the new repository

**Files:**
- Create: `<repo>/` with `pyproject.toml`, `requirements.txt`, `requirements-dev.txt`, `.python-version`, `package.json`, `Makefile`, `LICENSE`, `.gitignore`, `README.md` (stub), `.github/workflows/ci.yml`, `docs/superpowers/specs/2026-11-02-lab-janitor-portfolio-design.md` (copy), `docs/superpowers/plans/2026-11-02-plan-1-bootstrap-and-detector.md` (copy), `tests/conftest.py`, `tests/unit/test_smoke.py`
- Create: `src/shared/__init__.py`, `src/detector/__init__.py`, `src/notifier/__init__.py`, `lab_janitor/__init__.py`, `tests/__init__.py`, `tests/unit/__init__.py`, `tests/infra/__init__.py`

**Interfaces:**
- Produces: `make install`, `make lint`, `make fmt`, `make test`, `make synth` targets; pytest `pythonpath = ["src", "."]`; `tests/conftest.py` sets the environment variables every handler test needs (see Step 4).

- [ ] **Step 1: Create the repo and the interpreter**

```bash
mkdir -p <repo> && cd <repo> && git init -b main
uv python install 3.13 && uv venv --python 3.13 .venv
```

Expected: `.venv/bin/python --version` prints `Python 3.13.x`.

- [ ] **Step 2: Write the packaging files**

`pyproject.toml`: project name `aws-lab-janitor`, `requires-python = ">=3.13,<3.14"`, `[tool.ruff] target-version = "py313"`, `line-length = 100`, `extend-exclude = ["cdk.out", ".venv"]`; `[tool.ruff.lint] select = ["E","F","I","UP","B","BLE","S","PIE","PL","RUF"]`, `per-file-ignores = {"tests/*" = ["S101","PLR2004"]}`; `[tool.ruff.lint.isort] known-first-party = ["shared","detector","notifier","lab_janitor"]`; `[tool.pytest.ini_options] pythonpath = ["src", "."]`, `testpaths = ["tests"]`.

`requirements.txt`: `aws-cdk-lib==2.275.0`, `constructs==10.7.1`. `requirements-dev.txt`: `pytest==8.4.2`, `boto3==1.43.123`, `ruff==0.17.3`, `cdk-nag==3.0.2`, `cfn-lint==1.59.0`. `.python-version`: `3.13`. `package.json`: `{"private": true, "devDependencies": {"aws-cdk": "2.1133.0"}}`.

`Makefile` targets: `install` (`uv pip install -r requirements.txt -r requirements-dev.txt` into `.venv`, `npm ci`), `lint` (`ruff check . && ruff format --check .`), `fmt`, `test` (`pytest -q`), `synth` (`npx cdk synth --quiet`), `nag` (`npx cdk synth --strict`).

`LICENSE`: MIT, year 2026, holder `Mateo Cortés`. `.gitignore`: `.venv/`, `node_modules/`, `cdk.out/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.ruff_cache/`, `cdk.context.json`, `.coverage`.

`README.md` stub: title `aws-lab-janitor`, one sentence, "Work in progress; see docs/superpowers/specs". (Plan 4 writes the real README.)

- [ ] **Step 3: Copy the spec and this plan**

```bash
mkdir -p docs/superpowers/specs docs/superpowers/plans
cp <old-repo>/docs/superpowers/specs/2026-11-02-lab-janitor-portfolio-design.md docs/superpowers/specs/
cp <old-repo>/docs/superpowers/plans/2026-11-02-plan-1-bootstrap-and-detector.md docs/superpowers/plans/
```

- [ ] **Step 4: Write `tests/conftest.py` and a smoke test**

`conftest.py`: a session-scoped autouse fixture that sets `AWS_DEFAULT_REGION=us-east-1`, `AWS_ACCESS_KEY_ID=testing`, `AWS_SECRET_ACCESS_KEY=testing`, `LAB_NAME_PREFIX=LAB-`, `LAB_TAG_KEY=Purpose`, `LAB_TAG_VALUE=lab`, `KEEP_TAG_KEY=janitor`, `KEEP_TAG_VALUE=keep`, `PLANS_BUCKET=test-plans`, `AUDIT_TABLE=test-audit`, `REPORTS_TOPIC_ARN=arn:aws:sns:us-east-1:123456789012:reports`, `MIN_AGE_DAYS=7`.

```python
# tests/unit/test_smoke.py
def test_python_version():
    import sys
    assert sys.version_info[:2] == (3, 13)
```

- [ ] **Step 5: Write `.github/workflows/ci.yml`**

On `push` and `pull_request`: `actions/checkout@v4`; `actions/setup-python@v5` with `python-version-file: .python-version` and `cache: pip`; `actions/setup-node@v4` with `node-version: 22`; `pip install -r requirements.txt -r requirements-dev.txt`; `npm ci`; `ruff check . && ruff format --check .`; `pytest -q`; `npx cdk synth --quiet -c reportEmail=ci@example.com` with env `CDK_DEFAULT_ACCOUNT=123456789012`, `CDK_DEFAULT_REGION=us-east-1`; `cfn-lint --ignore-checks W3005 -- cdk.out/*.template.json`. The synth and cfn-lint steps are added in this task but will only pass after Task 13; mark them `continue-on-error: true` now and remove that flag in Task 13.

- [ ] **Step 6: Install and run**

Run: `make install && make lint && make test`
Expected: ruff clean, `1 passed`.

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "chore: bootstrap aws-lab-janitor repository

Python 3.13 project with ruff, pytest, pinned CDK toolchain, MIT license,
CI skeleton, and the approved design document and plan."
```

---

### Task 2: Settings and marking helpers

**Files:**
- Create: `src/shared/settings.py`, `src/shared/marking.py`
- Test: `tests/unit/test_settings.py`, `tests/unit/test_marking.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Settings` with fields `dry_run: bool`, `min_age_days: int`, `name_prefix: str`, `tag_key: str`, `tag_value: str`, `keep_tag_key: str`, `keep_tag_value: str`, `plans_bucket: str`, `audit_table: str`, `reports_topic_arn: str`, `region: str`.
  - `load_settings(event: dict, env: Mapping[str, str]) -> Settings`: `dry_run` is `False` only when `event["config"]["dryRun"] is False` (boolean); `min_age_days` from `event["config"]["minAgeDays"]` when `isinstance(v, int) and not isinstance(v, bool) and v >= 0`, else `int(env.get("MIN_AGE_DAYS", "7"))`; markers from `env` (required); `plans_bucket`, `audit_table`, `reports_topic_arn` with `env.get(name, "")` (empty when absent, so the detector does not need the topic ARN); raises `ValueError` if `name_prefix` shorter than 3 characters or any marker empty; `region` from `env["AWS_REGION"]` falling back to `us-east-1`.
  - `tag_value(tags: list[dict] | None, key: str) -> str | None` (accepts `{"Key","Value"}` and `{"key","value"}` shapes).
  - `is_lab(name: str | None, tags: list[dict] | None, settings: Settings) -> bool` (prefix on name, case-sensitive, OR tag equals).
  - `is_lab_tagged(tags, settings) -> bool` (tag only).
  - `is_kept(tags, settings) -> bool`.
  - `age_days(timestamp: datetime | None, now: datetime) -> int | None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_settings.py
import os, pytest
from shared.settings import load_settings

def test_defaults_to_dry_run_without_config():
    assert load_settings({}, os.environ).dry_run is True

def test_string_false_stays_dry_run():
    assert load_settings({"config": {"dryRun": "false"}}, os.environ).dry_run is True

def test_boolean_false_disables_dry_run():
    assert load_settings({"config": {"dryRun": False}}, os.environ).dry_run is False

def test_min_age_from_config_overrides_env():
    assert load_settings({"config": {"minAgeDays": 0}}, os.environ).min_age_days == 0
    assert load_settings({}, os.environ).min_age_days == 7

def test_empty_prefix_is_rejected():
    env = dict(os.environ, LAB_NAME_PREFIX="")
    with pytest.raises(ValueError):
        load_settings({}, env)

def test_missing_optional_env_does_not_raise():
    env = {k: v for k, v in os.environ.items() if k not in ("REPORTS_TOPIC_ARN", "PLANS_BUCKET", "AUDIT_TABLE")}
    assert load_settings({}, env).reports_topic_arn == ""

def test_boolean_min_age_is_ignored():
    assert load_settings({"config": {"minAgeDays": True}}, os.environ).min_age_days == 7
```

```python
# tests/unit/test_marking.py (uses a `settings` fixture built with load_settings({}, os.environ))
def test_prefix_is_case_sensitive(settings):
    assert is_lab("LAB-x", None, settings) and not is_lab("lab-x", None, settings)

def test_tag_marks_lab_but_not_candidate_helpers(settings):
    tags = [{"Key": "Purpose", "Value": "lab"}]
    assert is_lab("prod-db", tags, settings) and is_lab_tagged(tags, settings)
    assert not is_lab_tagged([{"Key": "Purpose", "Value": "Lab"}], settings)

def test_keep_tag(settings):
    assert is_kept([{"Key": "janitor", "Value": "keep"}], settings)

def test_age_days():
    now = datetime(2026, 11, 10, tzinfo=timezone.utc)
    assert age_days(datetime(2026, 11, 1, tzinfo=timezone.utc), now) == 9
    assert age_days(None, now) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/unit/test_settings.py tests/unit/test_marking.py -q`
Expected: FAIL with `ModuleNotFoundError: shared.settings`.

- [ ] **Step 3: Implement `settings.py` and `marking.py` with the signatures above**

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/unit/test_settings.py tests/unit/test_marking.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/shared tests/unit/test_settings.py tests/unit/test_marking.py
git commit -m "feat(shared): settings loader with safe dry-run default and lab marking helpers"
```

---

### Task 3: Client factory, pricing table and finding types

**Files:**
- Create: `src/shared/aws_clients.py`, `src/shared/pricing.py`, `src/shared/findings.py`
- Test: `tests/unit/test_aws_clients.py`, `tests/unit/test_pricing.py`

**Interfaces:**
- Produces:
  - `class ClientFactory`: `__init__(self, session: boto3.Session | None = None)`; `get(self, service: str, region: str) -> BaseClient` (one shared session, a `threading.Lock` around client creation, a cache keyed by `(service, region)`, `botocore.config.Config(connect_timeout=5, read_timeout=20, retries={"mode": "standard", "max_attempts": 3})`); `enabled_regions(self) -> list[str]` (`describe_regions(AllRegions=False)` from `us-east-1`, sorted).
  - `FIXED_HOURLY_USD: dict[str, float]` with keys `nat-gateway: 0.045`, `public-ipv4: 0.005`, `alb: 0.0225`, `nlb: 0.0225`, `ebs-gp3-gib: 0.08/730`, `ebs-snapshot-gib: 0.05/730`, `vpc-endpoint-interface-az: 0.01`; `PRICES_AS_OF = "2026-11"`, `PRICES_REGION = "us-east-1"`; `monthly_usd(kind: str, units: float = 1.0) -> float | None` (hourly times 730 times units, rounded to 2 decimals, `None` for unknown kinds).
  - `@dataclass class Finding`: `region: str`, `kind: str`, `resource_id: str`, `arn: str | None`, `detail: str`, `lab: bool`, `candidate: bool`, `reason: str`, `age_days: int | None`, `estimated_monthly_usd: float | None`, `tags: list[dict]` (default empty). Method `to_dict() -> dict` with camelCase keys (`resourceId`, `ageDays`, `estimatedMonthlyUsd`), tags omitted.
  - `@dataclass class ErrorFinding`: `region: str`, `service: str`, `error: str`, `classification: str`; `to_dict()`.
  - Kind strings used by every detector (exact): `ec2-instance`, `nat-gateway`, `elastic-ip`, `public-ipv4`, `ebs-volume`, `ebs-snapshot`, `ami`, `vpc-endpoint`, `elbv2`, `rds-instance`, `rds-cluster`, `elasticache`, `internet-monitor`, `s3-bucket`, `cloudfront`, `cw-dashboard`.

- [ ] **Step 1: Write the failing tests**

```python
def test_client_config_and_cache():
    f = ClientFactory()
    c1, c2 = f.get("ec2", "us-east-1"), f.get("ec2", "us-east-1")
    assert c1 is c2
    assert c1.meta.config.connect_timeout == 5 and c1.meta.config.read_timeout == 20
    # botocore stores max_attempts=3 as total_max_attempts=4 on the built client
    assert c1.meta.config.retries == {"mode": "standard", "total_max_attempts": 4}

def test_monthly_usd():
    assert monthly_usd("nat-gateway") == 32.85
    assert monthly_usd("public-ipv4", 3) == 10.95
    assert monthly_usd("unknown") is None
```

- [ ] **Step 2: Run to verify they fail** (`ModuleNotFoundError`)

- [ ] **Step 3: Implement the three modules**

- [ ] **Step 4: Run to verify they pass**

- [ ] **Step 5: Commit** with message `feat(shared): client factory with timeouts, pricing table and finding types`.

---

### Task 4: Sweep framework and EC2 detectors

**Files:**
- Create: `src/detector/sweep.py`, `src/detector/detectors/__init__.py`, `src/detector/detectors/ec2.py`
- Test: `tests/unit/test_sweep.py`, `tests/unit/test_detect_ec2.py`

**Interfaces:**
- Consumes: Task 2 and Task 3 interfaces.
- Produces:
  - `classify_error(exc: BaseException) -> str` returning one of `access-denied` (ClientError codes `AccessDenied`, `AccessDeniedException`, `UnauthorizedOperation`, `OptInRequired`), `service-unavailable-in-region` (`EndpointConnectionError`, `ClientError` with HTTP status 404 or code `InvalidAction`, `UnknownServiceError`), `throttled` (`Throttling`, `ThrottlingException`, `RequestLimitExceeded`, `TooManyRequestsException`), `time-budget`, else `other`.
  - `safe(ctx: SweepContext, errors: list[ErrorFinding], region: str, service: str, fn: Callable[[], None]) -> None`: runs `fn`, on exception appends `ErrorFinding(region, service, str(exc)[:160], classify_error(exc))` and logs a warning; never raises.
  - `@dataclass class SweepContext`: fields `settings: Settings`, `clients: ClientFactory`, `now: datetime`, `account: str`, `was_proposed_live: Callable[[str], bool]`, `deadline: float` (epoch seconds from `time.time()`), `log: logging.LoggerAdapter` (carries `runId`; tests pass `logging.LoggerAdapter(logging.getLogger("test"), {"runId": "test"})`). `safe` takes the context as its first argument: `safe(ctx, errors, region, service, fn)`; when `time.time() > ctx.deadline` it does not call `fn` and records an `ErrorFinding` with classification `time-budget` and message `skipped: time budget exhausted`.
  - `collect(client, op: str, key: str, **params) -> list` (paginates when `can_paginate`; `key` may be a dotted path such as `DistributionList.Items`).
  - `detect_ec2(region: str, ctx: SweepContext, findings: list[Finding], errors: list[ErrorFinding]) -> None` with one `safe` block per call: instances (non-terminated; detail includes type, state, name, `PublicIpAddress` when present; age from `LaunchTime`), NAT gateways (`available`; `nat-gateway` price), addresses (kind `elastic-ip`, `resource_id = AllocationId` (PublicIp only for legacy addresses without one, never candidates); every address costs `public-ipv4`; `candidate` only if no `AssociationId`, has `AllocationId`, `is_lab_tagged`, not kept, and `ctx.was_proposed_live(allocation_id)`; reason `"idle, tagged lab, proposed in a previous live run"`), volumes (list every volume with no `Attachments`; candidate requires `State == "available"`, `is_lab_tagged`, not kept, `age_days >= min_age_days` from `CreateTime`; cost `ebs-gp3-gib` times `Size`), snapshots (`OwnerIds=["self"]`; first load `describe_images(Owners=["self"])` and build `ami_snapshots: dict[snapshot_id, image_id]`; candidate requires tag, not kept, age, and not in `ami_snapshots`; a snapshot backing an AMI gets detail suffix `backs AMI <id>` and `reason = "backs AMI <id>"`), AMIs (kind `ami`, never candidate), interface VPC endpoints (`VpcEndpointType == "Interface"`, units = number of `SubnetIds`, price `vpc-endpoint-interface-az`), public IPv4 via `describe_network_interfaces` (count ENIs whose `Association` has `PublicIp` but no `AllocationId`, since those with an `AllocationId` are Elastic IPs already reported; one aggregated `public-ipv4` finding per region, `resource_id = f"public-ipv4-{region}"`, `detail = f"{n} auto-assigned public IPv4 addresses"`, cost `monthly_usd("public-ipv4", n)`, omitted when `n == 0`).
  - Every finding's `lab` uses `is_lab(name_tag, tags)`; `reason` for non-candidates that are lab-tagged but blocked: `"below minimum age (N days)"`, `"tagged janitor=keep"`, `"attached"`, `"associated"`.

- [ ] **Step 1: Write the failing tests** (Stubber on real boto3 clients created by a `ClientFactory` whose `get` is monkeypatched to return the stubbed client for `("ec2", "us-east-1")`)

```python
def test_safe_records_service_unavailable(ctx):
    errors = []
    def boom():
        raise EndpointConnectionError(endpoint_url="x")
    safe(ctx, errors, "ap-east-2", "internetmonitor:ListMonitors", boom)
    assert errors[0].classification == "service-unavailable-in-region"

def test_access_denied_in_one_call_does_not_stop_the_others(ec2_stub, ctx):
    # describe_instances raises UnauthorizedOperation; describe_volumes returns one unattached volume
    ...
    assert [e.service for e in errors] == ["ec2:DescribeInstances"]
    assert any(f.kind == "ebs-volume" for f in findings)

def test_snapshot_rules(ec2_stub, ctx):
    # three lab-tagged snapshots: 42 days old (candidate), 2 days old (not), 42 days old but in an AMI (not),
    # plus one 42 days old tagged janitor=keep (not)
    cands = [f for f in findings if f.kind == "ebs-snapshot" and f.candidate]
    assert [f.resource_id for f in cands] == ["snap-old"]
    reasons = {f.resource_id: f.reason for f in findings if f.kind == "ebs-snapshot"}
    assert reasons["snap-young"].startswith("below minimum age")
    assert reasons["snap-ami"] == "backs AMI ami-1"
    assert reasons["snap-keep"] == "tagged janitor=keep"

def test_eip_requires_previous_live_proposal(ec2_stub, ctx_factory):
    # idle lab-tagged EIP; ctx.was_proposed_live returns False -> not candidate; True -> candidate
    ...

def test_associated_eip_is_never_candidate_but_costs(ec2_stub, ctx):
    assert f.candidate is False and f.estimated_monthly_usd == 3.65

def test_time_budget_skips_calls(ctx_past_deadline):
    assert errors[0].classification == "time-budget"
```

- [ ] **Step 2: Run to verify they fail**

- [ ] **Step 3: Implement `sweep.py` and `detectors/ec2.py`**

Each sub-detector is a nested function invoked as `safe(ctx, errors, region, "ec2:DescribeInstances", run_instances)` so the error finding names the exact operation. `safe(ctx, errors, region, service, fn)` is the only signature; Tasks 5 and 6 use it unchanged.

- [ ] **Step 4: Run to verify they pass**

- [ ] **Step 5: Commit** `feat(detector): sweep framework with error classification and EC2 detectors`.

---

### Task 5: Database, load balancer and monitor detectors

**Files:**
- Create: `src/detector/detectors/databases.py`, `src/detector/detectors/elb.py`, `src/detector/detectors/monitor.py`
- Test: `tests/unit/test_detect_databases.py`, `tests/unit/test_detect_elb.py`, `tests/unit/test_detect_monitor.py`

**Interfaces:**
- Consumes: Task 4 `SweepContext`, `safe`, `collect`; Task 2 marking.
- Produces: `detect_rds(region, ctx, findings, errors)`, `detect_elasticache(...)`, `detect_elbv2(...)`, `detect_internet_monitor(...)`, same signature as `detect_ec2`.
  - RDS instances: `is_lab(identifier, db["TagList"])`; detail includes class, engine, `DBInstanceStatus`; age from `InstanceCreateTime`; never candidate. RDS clusters via `describe_db_clusters`: kind `rds-cluster`; when `DBClusterMembers` is empty detail starts with `no instances: storage and backups still billed`; tags from `TagList`.
  - ElastiCache: `list_tags_for_resource(ResourceName=cluster["ARN"])` inside its own `safe` per cluster; on failure fall back to prefix-only marking and record the error.
  - ELBv2: `describe_tags` in batches of 20 ARNs; `lab` from name prefix or tags; cost `alb`/`nlb` by `Type` (`gateway` has no price, `estimated_monthly_usd=None`); age from `CreatedTime`.
  - Internet Monitor: `list_monitors`; `lab` by name prefix; tags via `list_tags_for_resource(ResourceArn=...)` only for `LAB-` monitors (keep check); candidate when prefix matches and not kept; reason `"name prefix LAB-"`; detail = status.

- [ ] **Step 1: Write the failing tests**

```python
def test_rds_tag_marks_lab_without_prefix(rds_stub, ctx):
    # DBInstanceIdentifier "postgres-demo" with TagList Purpose=lab
    assert finding.lab is True and finding.candidate is False

def test_headless_cluster_is_reported(rds_stub, ctx):
    assert finding.kind == "rds-cluster" and finding.detail.startswith("no instances")

def test_elbv2_tags_in_batches_of_20(elbv2_stub, ctx):
    # 25 load balancers -> describe_tags called with 20 then 5 ARNs (Stubber enforces params)

def test_monitor_keep_tag_blocks_candidate(im_stub, ctx):
    ...
```

- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement the three modules**
- [ ] **Step 4: Run to verify they pass**
- [ ] **Step 5: Commit** `feat(detector): RDS, Aurora clusters, ElastiCache, ELBv2 and Internet Monitor detectors with tag-based marking`.

---

### Task 6: Global detectors

**Files:**
- Create: `src/detector/detectors/global_.py`
- Test: `tests/unit/test_detect_global.py`

**Interfaces:**
- Produces: `detect_global(ctx, findings, errors) -> None` with region `"global"`: S3 buckets (skip names starting with `cdk-`; tags via `get_bucket_tagging`, `NoSuchTagSet` treated as no tags; because bucket names are lowercase, for S3 only the prefix check is case-insensitive: `name.lower().startswith(settings.name_prefix.lower())`, tag check unchanged; age from `CreationDate`), CloudFront distributions (paginated through `DistributionList.Items`; detail `domain enabled=<bool>`; `lab` False; cost `None`), CloudWatch dashboards (`list_dashboards`; `lab` by prefix; keep tag via `list_tags_for_resource(ResourceARN=arn)` only for `LAB-` dashboards; candidate when prefix and not kept; reason `"name prefix LAB-"`; `arn = f"arn:aws:cloudwatch::{ctx.account}:dashboard/{name}"`). The handler (Task 9) fills `ctx.account` from `sts.get_caller_identity`.

- [ ] **Step 1: Write the failing tests**

```python
def test_cdk_buckets_are_skipped_and_tags_read(s3_stub, ctx): ...
def test_cloudfront_pagination(cf_stub, ctx):  # two pages, 3 distributions total
def test_dashboard_candidate_and_keep(cw_stub, ctx): ...
```

- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement `global_.py`**
- [ ] **Step 4: Run all unit tests** (`pytest tests/unit -q`), expected all PASS.
- [ ] **Step 5: Commit** `feat(detector): global detectors for S3, CloudFront and CloudWatch dashboards`.

---

### Task 7: Plan builder and report renderer

**Files:**
- Create: `src/shared/plan.py`, `src/shared/report.py`
- Test: `tests/unit/test_plan.py`, `tests/unit/test_report.py`

**Interfaces:**
- Consumes: `Finding`, `ErrorFinding`, `Settings`.
- Produces:
  - `new_run_id(now: datetime) -> str` formatted `YYYYMMDDTHHMMSSZ-<4 hex>` (example `20261110T140012Z-7f3a`).
  - `build_plan(run_id: str, settings: Settings, findings: list[Finding], errors: list[ErrorFinding], regions: list[str], generated_at: datetime) -> dict` with keys exactly: `runId`, `generatedAt` (ISO 8601 UTC), `dryRun`, `criteria` (`tagKey`, `tagValue`, `namePrefix`, `keepTag` as `"janitor=keep"`, `minAgeDays`), `regions`, `candidates` (findings with `candidate` true, `to_dict()`), `inventory` (the rest), `errors`, `estimatedMonthlyUsd` (sum over all findings, 2 decimals), `pricesAsOf`, `pricesRegion`.
  - `plan_bytes(plan: dict) -> bytes` (`json.dumps(sort_keys=True, separators=(",", ":"))`) and `plan_sha256(data: bytes) -> str` (hex).
  - `render_report(plan: dict, bucket: str) -> str` (the bucket is not part of the plan; Task 9 passes `settings.plans_bucket`, tests pass a literal) with sections in order: header (title `LAB JANITOR WEEKLY REPORT`, run id, mode `DRY RUN` or `LIVE`, regions swept, findings count, candidates count, `Estimated fixed monthly cost of everything detected: ~$X`), `ACTIONABLE` (one line per candidate: `[kind] id (region) age reason ~$/mo`), `INVENTORY` grouped by kind with one line per resource, `ERRORS` (one line per error `region service classification: message`, omitted when empty), `REGIONS WITH NOTHING FOUND` (comma-separated), footer `Full plan: s3://<bucket>/plans/<runId>/plan.json`. The line `ACCOUNT CLEAN: no billable resources detected` appears only when there are no findings **and** no errors.
  - `truncate_report(text: str, limit_bytes: int, pointer: str) -> str`: returns `text` unchanged if it fits, else the longest prefix of whole lines under `limit_bytes` plus `\n... truncated, N lines omitted. Full report: <pointer>`.

- [ ] **Step 1: Write the failing tests**

```python
def test_plan_shape_and_hash_stability(): ...   # same inputs -> same sha256; keys exactly as listed
def test_account_clean_only_without_errors(): ...
def test_candidates_section_lists_reason_and_cost(): ...
def test_truncate_keeps_whole_lines_under_limit():
    text = "\n".join(f"line {i}" for i in range(50_000))
    out = truncate_report(text, 150_000, "s3://b/k")
    assert len(out.encode()) <= 150_000 and out.endswith("Full report: s3://b/k")
```

- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement `plan.py` and `report.py`**
- [ ] **Step 4: Run to verify they pass**
- [ ] **Step 5: Commit** `feat(shared): immutable plan builder with sha256 and sectioned report renderer`.

---

### Task 8: Plan store and audit client

**Files:**
- Create: `src/shared/plan_store.py`, `src/shared/audit.py`
- Test: `tests/unit/test_plan_store.py`, `tests/unit/test_audit.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class StoredPlan`: `plan_key: str`, `plan_version_id: str`, `plan_sha256: str`, `report_key: str`, `report_version_id: str`.
  - `class PlanStore`: `__init__(self, s3_client, bucket: str)`; `put(self, run_id: str, plan: dict, report: str) -> StoredPlan` (keys `plans/<runId>/plan.json` and `plans/<runId>/report.txt`, `ContentType` `application/json` / `text/plain; charset=utf-8`); `get_report(self, run_id: str, version_id: str) -> str`; `get_plan(self, run_id: str, version_id: str) -> tuple[dict, str]` returning the plan and the sha256 of the bytes read.
  - `class AuditClient`: `__init__(self, ddb_client, table_name: str)` (low-level client; items are serialized with `boto3.dynamodb.types.TypeSerializer` after converting floats to `Decimal(str(x))` and dropping `None` values); `put_run_event(self, run_id: str, region: str, resource_id: str, event: str, actor: str, dry_run: bool, detail: dict, at: datetime) -> None` writing `pk = f"RUN#{run_id}"`, `sk = f"RESOURCE#{region}#{resource_id}#{event}"`, attributes `event`, `at` (ISO), `actor`, `dryRun`, `resourceId`, `detail` (map), with `ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)"`; swallows `ConditionalCheckFailedException` with a warning log; `was_proposed_live(self, resource_id: str) -> bool` calling `query` with exactly `IndexName="by-resource"`, `KeyConditionExpression="resourceId = :id"`, `FilterExpression="#e = :e AND dryRun = :f"`, `ExpressionAttributeNames={"#e": "event"}`, `ExpressionAttributeValues={":id": {"S": resource_id}, ":e": {"S": "PROPOSED"}, ":f": {"BOOL": False}}`, paging on `LastEvaluatedKey`, returning `True` on the first item found.
  - `put_run_summary(self, run_id: str, summary: dict, at: datetime)` writing `sk = "RUN#SUMMARY"`.

- [ ] **Step 1: Write the failing tests** (Stubber: assert exact `Key`, `Bucket`, `ConditionExpression` and `IndexName` params)

```python
def test_put_writes_both_objects_and_returns_versions(): ...
def test_put_run_event_is_conditional(): ...
def test_conditional_failure_is_swallowed(): ...
def test_was_proposed_live_filters_dry_run(): ...
```

- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement both modules**
- [ ] **Step 4: Run to verify they pass**
- [ ] **Step 5: Commit** `feat(shared): versioned plan store and append-only audit client`.

---

### Task 9: Detector handler

**Files:**
- Create: `src/detector/handler.py`
- Test: `tests/unit/test_detector_handler.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `handler(event: dict, context) -> dict` returning exactly `{"runId", "planKey", "planVersionId", "planSha256", "reportKey", "reportVersionId", "candidateCount", "errorCount", "findingCount", "estimatedMonthlyUsd", "dryRun"}`. Behavior: `settings = load_settings(event, os.environ)`; `run_id = new_run_id(now)`; `account` from STS; regions from `ClientFactory.enabled_regions()`; `deadline = time.time() + (context.get_remaining_time_in_millis()/1000) - 45` (45 s reserve for plan upload and audit writes; when `context` is `None` use 240 s); regions swept in a `ThreadPoolExecutor(max_workers=12)` by `sweep_region(region, ctx) -> tuple[list[Finding], list[ErrorFinding]]`, which first checks the deadline: when `time.time() > ctx.deadline` it returns a single `ErrorFinding(region, "sweep", "skipped: time budget exhausted", "time-budget")` without calling any detector, otherwise calls `detect_ec2`, `detect_rds`, `detect_elasticache`, `detect_elbv2`, `detect_internet_monitor`; the same guard runs before `detect_global` (region `"global"`); plan and report built (`render_report(plan, settings.plans_bucket)`), `print`-free: logging only; `PlanStore.put`; one `DETECTED` audit event per finding and one `PROPOSED` per candidate (`actor = "lambda:detector"`, `detail = {"kind","reason","estimatedMonthlyUsd","planSha256"}`); `put_run_summary`. Logging: `logger = logging.LoggerAdapter(logging.getLogger("detector"), {"runId": run_id})` created right after `new_run_id` and stored on the `SweepContext` (add field `log: logging.LoggerAdapter` in Task 4; `safe` logs through `ctx.log.warning`), so every record carries `runId` in the JSON output. Module-level `build_dependencies(settings) -> tuple[ClientFactory, PlanStore, AuditClient]` that tests monkeypatch. `if __name__ == "__main__":` builds settings from `{"config": {"dryRun": True}}`, runs the sweep and `build_plan` with local credentials, and prints `plan_bytes(plan)` to stdout **without** calling `PlanStore` or `AuditClient` (no upload, no audit writes, no executor exists locally).

- [ ] **Step 1: Write the failing tests** (monkeypatch `build_dependencies`, the five `detect_*` functions and `enabled_regions`; use fakes for `PlanStore` and `AuditClient` that record calls)

```python
def test_result_is_compact_and_matches_plan(): ...   # no 'candidates' or 'report' keys in the result
def test_audit_events_detected_and_proposed(): ...   # 3 findings, 1 candidate -> 3 DETECTED + 1 PROPOSED + summary
def test_region_failure_becomes_error_not_crash(): ... # detect_ec2 raising -> errorCount 1, plan still stored
def test_time_budget_exhausted_still_writes_plan(): ... # fake context with get_remaining_time_in_millis() == 1000 -> deadline in the past -> every region and global produce one time-budget error, detectors never called, plan still stored
def test_missing_config_is_dry_run(): ...
```

- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement `handler.py`**
- [ ] **Step 4: Run to verify they pass**; also run `.venv/bin/python -c "import detector.handler"` with no credentials, expected no error.
- [ ] **Step 5: Commit** `feat(detector): handler orchestrating the sweep, plan upload and audit events`.

---

### Task 10: Notifier handler (report mode)

**Files:**
- Create: `src/notifier/handler.py`
- Test: `tests/unit/test_notifier_handler.py`

**Interfaces:**
- Consumes: `PlanStore.get_report`, `truncate_report`.
- Produces: `handler(event: dict, context) -> dict`. Event shape: `{"mode": "report", "detect": <Detect result>, "config": {...}}`. Reads the report at `reportVersionId`, truncates to `150_000` bytes with pointer `s3://<bucket>/<reportKey>`, publishes to `REPORTS_TOPIC_ARN` with subject `f"[Lab Janitor] Weekly report: {candidateCount} candidates, ~${estimatedMonthlyUsd}/month{' (DRY RUN)' if dryRun else ''}"` (assert under 100 characters; fall back to `[Lab Janitor] Weekly report (DRY RUN)` if longer), returns `{"published": True, "messageId": ...}`. Unknown `mode` raises `ValueError` (modes `approval` and `result` arrive in Plan 2). Module exposes `build_dependencies() -> tuple[PlanStore, sns_client]` for tests.

- [ ] **Step 1: Write the failing tests** (Stubber for SNS `publish` asserting `Subject`, `TopicArn`, `Message` size)

```python
def test_report_mode_publishes_truncated_report(): ...   # 300 KB report -> Message under 150 000 bytes with pointer
def test_subject_marks_dry_run_and_stays_short(): ...
def test_unknown_mode_raises(): ...
```

- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run to verify they pass**
- [ ] **Step 5: Commit** `feat(notifier): report mode publishing the truncated weekly report`.

---

### Task 11: CDK config, markers, notifications and audit store

**Files:**
- Create: `lab_janitor/markers.py`, `lab_janitor/config.py`, `lab_janitor/notifications.py`, `lab_janitor/audit.py`, `lab_janitor/stack.py`, `app.py`, `cdk.json`
- Test: `tests/infra/conftest.py`, `tests/infra/test_config.py`, `tests/infra/test_notifications.py`, `tests/infra/test_audit_store.py`

**Interfaces:**
- Produces:
  - `markers.py` constants listed in Global Constraints plus `def lambda_env() -> dict[str, str]` returning `LAB_NAME_PREFIX`, `LAB_TAG_KEY`, `LAB_TAG_VALUE`, `KEEP_TAG_KEY`, `KEEP_TAG_VALUE`.
  - `@dataclass(frozen=True) class JanitorConfig`: `report_email: str`, `alert_email: str`, `dry_run: bool`, `min_age_days: int`, `schedule_timezone: str`, `schedule_hour: int`, `janitor_enabled: bool`; `JanitorConfig.from_node(node: constructs.Node) -> JanitorConfig` reading context keys `reportEmail` (or env `JANITOR_REPORT_EMAIL`; missing raises `ValueError("Set -c reportEmail=you@example.com")`), `alertEmail` (default report email), `dryRun` (default `True`; accepts `"false"`/`False` to disable), `minAgeDays` (7), `scheduleTimezone` (`America/Bogota`), `scheduleHour` (9), `janitorEnabled` (`True`).
  - The DLQ is created in `stack.py` in this task: `sqs.Queue(self, "Dlq", retention_period=Duration.days(14), encryption=sqs.QueueEncryption.SQS_MANAGED, enforce_ssl=True)`, passed to `Notifications` now and to `Workflow` in Task 13.
  - `class Notifications(Construct)`: props `config`, `dlq: sqs.IQueue`; attributes `reports_topic: sns.Topic`, `alerts_topic: sns.Topic`; both topics `enforce_ssl=True`; email subscriptions with `dead_letter_queue=dlq`; display names `Lab Janitor Reports` and `Lab Janitor Alerts`.
  - `class AuditStore(Construct)`: attributes `table: dynamodb.TableV2` (partition `pk` string, sort `sk` string, `billing=dynamodb.Billing.on_demand()`, `point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True)`, GSI `by-resource` with partition `resourceId` and sort `at`, `time_to_live_attribute="ttl"`, `removal_policy=RETAIN`, no explicit table name so two stacks can coexist in one account), `plans_bucket: s3.Bucket` (`versioned=True`, `block_public_access=BLOCK_ALL`, `object_ownership=BUCKET_OWNER_ENFORCED`, `enforce_ssl=True`, `removal_policy=RETAIN`, lifecycle: expiration 400 days on prefix `plans/`, `noncurrent_version_expiration` 30 days) with a policy statement Deny `s3:DeleteObject`, `s3:DeleteObjectVersion` on `plans/*` with `principals=[iam.StarPrincipal()]` (renders `"Principal": "*"`) and `StringNotEquals aws:PrincipalArn = arn:aws:iam::<account>:root`; method `allow_plan_writes(grantee_role_arn: str)` that adds Deny `s3:PutObject` on `plans/*` to `StarPrincipal` with `StringNotEquals aws:PrincipalArn = <role arn>` (called by the Detector construct in Task 12). Method `grant_run_event_writes(grantee: iam.IGrantable)`: `dynamodb:PutItem` on the table with condition `ForAllValues:StringLike dynamodb:LeadingKeys = ["RUN#*"]`; `grant_resource_queries(grantee)`: `dynamodb:Query` on exactly the table ARN and the `by-resource` index ARN, intentionally without a `LeadingKeys` condition (the GSI is keyed by `resourceId`).
  - `stack.py`: `class LabJanitorStack(Stack)` composing the DLQ, `Notifications`, `AuditStore`, `Detector` (Task 12), `Workflow` (Task 13); description `Lab janitor: weekly sweep of lab leftovers with plan, approval and audit trail`; tags `Project=aws-lab-janitor`, `ManagedBy=cdk`.
  - `app.py`: env from `CDK_DEFAULT_ACCOUNT`/`CDK_DEFAULT_REGION` (raise with a clear message if missing), stack id `LabJanitorStack`. No cdk-nag registration yet (Task 13 adds it together with the acknowledgments).
  - `tests/infra/conftest.py`: fixture `template` that synthesizes `LabJanitorStack` with `env=Environment(account="123456789012", region="us-east-1")` and context `reportEmail=test@example.com`, returning `assertions.Template`.

- [ ] **Step 1: Write the failing tests**

```python
def test_missing_report_email_fails_with_hint(): ...   # ValueError message contains "-c reportEmail"
def test_no_output_contains_email(template): ...       # no Outputs value contains "@"
def test_topics_enforce_ssl_and_subscriptions_have_dlq(template): ...
def test_plans_bucket_policy_denies_delete_except_root(template):
    # a Deny statement with "Principal": "*" on s3:DeleteObject and s3:DeleteObjectVersion
    # and Condition StringNotEquals aws:PrincipalArn ending in ":root"
def test_audit_table_has_gsi_pitr_and_ttl(template): ...
def test_dlq_is_encrypted_and_ssl_only(template): ...
```

- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement the constructs, the stack with the DLQ, Notifications and AuditStore only, and `app.py`** (the stack adds Detector and Workflow in later tasks)
- [ ] **Step 4: Run `pytest tests/infra -q`**, expected PASS; run `CDK_DEFAULT_ACCOUNT=123456789012 CDK_DEFAULT_REGION=us-east-1 npx cdk synth -c reportEmail=ci@example.com --quiet`, expected exit 0.
- [ ] **Step 5: Commit** `feat(cdk): config, markers, notification topics and audit store`.

---

### Task 12: CDK detector construct

**Files:**
- Create: `lab_janitor/detector.py`
- Modify: `lab_janitor/stack.py`
- Test: `tests/infra/test_detector.py`, `tests/infra/test_iam_guardrails.py`

**Interfaces:**
- Produces: `class Detector(Construct)` with props `config`, `audit: AuditStore`, `code: lambda_.Code` (the shared `Code.from_asset("src", exclude=["__pycache__", "*.pyc", "**/tests/*"])` created once in `stack.py`); attribute `function: lambda_.Function` (`runtime PYTHON_3_13`, `architecture ARM_64`, handler `detector.handler.handler`, `memory_size 512`, `timeout 5 minutes`, `reserved_concurrent_executions 1`, `log_group` explicit with `RetentionDays.THREE_MONTHS` and `RemovalPolicy.DESTROY`, `logging_format JSON`, environment from `markers.lambda_env()` plus `PLANS_BUCKET`, `AUDIT_TABLE`, `MIN_AGE_DAYS`). Role statement `DetectorReadOnlySweep` with exactly: `ec2:DescribeRegions, ec2:DescribeInstances, ec2:DescribeNatGateways, ec2:DescribeAddresses, ec2:DescribeVolumes, ec2:DescribeSnapshots, ec2:DescribeImages, ec2:DescribeVpcEndpoints, ec2:DescribeNetworkInterfaces, elasticloadbalancing:DescribeLoadBalancers, elasticloadbalancing:DescribeTags, rds:DescribeDBInstances, rds:DescribeDBClusters, rds:ListTagsForResource, elasticache:DescribeCacheClusters, elasticache:ListTagsForResource, internetmonitor:ListMonitors, internetmonitor:ListTagsForResource, cloudwatch:ListDashboards, cloudwatch:ListTagsForResource, s3:ListAllMyBuckets, s3:GetBucketTagging, cloudfront:ListDistributions, sts:GetCallerIdentity` on `*`; `s3:PutObject` on `plans/*`; `audit.grant_run_event_writes(function)`; `audit.grant_resource_queries(function)`; `audit.allow_plan_writes(function.role.role_arn)`. cdk-nag acknowledgments are added in Task 13.
  - Helpers for tests in `tests/infra/helpers.py`: `iam_statements(template) -> list[dict]` flattening every `AWS::IAM::Policy` and inline role policy statement; `actions(statement) -> list[str]`; `role_statements(template, handler: str) -> list[dict]`: finds the `AWS::Lambda::Function` whose `Handler` equals `handler`, reads the logical id in its `Role["Fn::GetAtt"][0]`, and returns the statements of every `AWS::IAM::Policy` whose `Roles` references that id plus the role's inline policies; `detector_statements(template)` is `role_statements(template, "detector.handler.handler")`.

- [ ] **Step 1: Write the failing tests**

```python
MUTATING_PREFIXES = ("Delete", "Release", "Terminate", "Create", "Put", "Update", "Tag", "Untag", "Modify", "Stop", "Start", "Reboot")

def test_detector_role_has_no_mutating_actions_outside_its_bucket_and_table(template):
    for st in detector_statements(template):
        for a in actions(st):
            if a.startswith(("s3:PutObject", "dynamodb:PutItem")): continue
            assert not a.split(":")[1].startswith(MUTATING_PREFIXES), a

def test_detector_dynamodb_write_grants_carry_leading_keys(template):
    # every Allow statement in detector_statements with dynamodb:PutItem or dynamodb:UpdateItem
    # has Condition ForAllValues:StringLike dynamodb:LeadingKeys == ["RUN#*"]
def test_detector_query_grant_is_limited_to_table_and_index(template):
    # the statement with dynamodb:Query has exactly two resources: the table ARN and .../index/by-resource
def test_plans_bucket_policy_denies_put_except_detector_role(template):
    # Deny s3:PutObject on plans/* with StringNotEquals aws:PrincipalArn referencing the detector role
def test_detector_function_settings(template):
    template.has_resource_properties("AWS::Lambda::Function", {"Runtime": "python3.13", "Architectures": ["arm64"], "ReservedConcurrentExecutions": 1, "Handler": "detector.handler.handler"})
def test_log_group_retention_90_days(template): ...
```

- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement `detector.py` and wire it in `stack.py`**
- [ ] **Step 4: Run `pytest tests/infra -q`**, expected PASS.
- [ ] **Step 5: Commit** `feat(cdk): detector function with enumerated read-only role`.

---

### Task 13: CDK workflow construct (dry-run path), alarms and nag clean synth

**Files:**
- Create: `lab_janitor/workflow.py`, `lab_janitor/notifier.py`
- Modify: `lab_janitor/stack.py`, `.github/workflows/ci.yml` (remove `continue-on-error`)
- Test: `tests/infra/test_workflow.py`, `tests/infra/test_snapshot.py`, `tests/snapshots/LabJanitorStack.template.json`

**Interfaces:**
- Produces:
  - `class Notifier(Construct)`: attribute `function` (same runtime settings as the detector but no reserved concurrency, memory 256, timeout 1 minute, handler `notifier.handler.handler`, env `PLANS_BUCKET`, `REPORTS_TOPIC_ARN`); grants `s3:GetObject` and `s3:GetObjectVersion` on `plans/*` and `reports_topic.grant_publish`.
  - `class Workflow(Construct)` props `config`, `detector: Detector`, `notifier: Notifier`, `notifications: Notifications`, `audit: AuditStore`, `dlq: sqs.IQueue`; attributes `state_machine: sfn.StateMachine` (`StateMachineType.STANDARD`, name-less, logs to its own `logs.LogGroup` at `ERROR` level, `RetentionDays.THREE_MONTHS`, `RemovalPolicy.DESTROY`), `schedule: scheduler.Schedule`, `alarms: list[cloudwatch.Alarm]`. Outputs: `CfnOutput(self, "StateMachineArn", value=state_machine.state_machine_arn)`, `CfnOutput(self, "PlansBucketName", value=audit.plans_bucket.bucket_name)`, `CfnOutput(self, "AuditTableName", value=audit.table.table_name)`; none carries an email.
    States (exact names): `Defaults` (`sfn.Pass` with `result=sfn.Result.from_object({"dryRun": True, "minAgeDays": config.min_age_days})`, `result_path="$.defaults"`); `Normalize` (`sfn.Pass` with `parameters={"config": sfn.JsonPath.json_merge(sfn.JsonPath.object_at("$.defaults"), sfn.JsonPath.execution_input)}`, which renders `States.JsonMerge($.defaults, $$.Execution.Input, false)`: any field present in the execution input overrides the default, a missing field keeps the default, and a string `"false"` stays a string that the Choice below never matches); `Detect` (`LambdaInvoke` detector, `payload = sfn.TaskInput.from_object({"config": sfn.JsonPath.object_at("$.config")})`, `payload_response_only=True`, `result_path = "$.detect"`, so `$.detect` is the handler's return value); `HasCandidates` (`Choice`: `sfn.Condition.and_(sfn.Condition.is_present("$.config.dryRun"), sfn.Condition.boolean_equals("$.config.dryRun", False), sfn.Condition.number_greater_than("$.detect.candidateCount", 0))` → `RequestApproval` placeholder which in this plan is a `Pass` named `ApprovalNotYetImplemented` that continues to `NotifyReportOnly`; otherwise `NotifyReportOnly`); `NotifyReportOnly` (`LambdaInvoke` notifier with payload `{"mode": "report", "detect.$": "$.detect", "config.$": "$.config"}`); `NotifyFailure` (`SnsPublish` to alerts with subject `[Lab Janitor] Run failed` and message `States.Format` of execution id, error and cause) followed by `sfn.Fail`. Every `LambdaInvoke` has `retry_on_service_exceptions=True` plus `add_retry(errors=["Lambda.TooManyRequestsException"], interval=Duration.seconds(30), backoff_rate=2, max_attempts=5)` and `add_catch(notify_failure, result_path="$.error")`.
    Schedule: `scheduler.Schedule` with `ScheduleExpression.cron(minute="0", hour=str(config.schedule_hour), week_day="MON", time_zone=TimeZone.of(config.schedule_timezone))` (`from aws_cdk import TimeZone`, the core module), target `scheduler_targets.StepFunctionsStartExecution(state_machine, input=ScheduleTargetInput.from_object({"dryRun": config.dry_run, "minAgeDays": config.min_age_days}), retry_attempts=2, max_event_age=Duration.hours(1), dead_letter_queue=dlq)`, `enabled=config.janitor_enabled`, description `Starts the lab janitor every Monday`.
    Alarms (OK actions to alerts too): `DlqNotEmpty` (`ApproximateNumberOfMessagesVisible`, Maximum, 5 min, `> 0`, `NOT_BREACHING`), `ExecutionFailed` (`MathExpression` `failed + timedOut` over `state_machine.metric_failed()` and `metric_timed_out()`, Sum, 5 min, `>= 1`, `NOT_BREACHING`). The heartbeat alarm arrives in Plan 3.
  - cdk-nag (this task registers the packs in `app.py` per Global Constraints and adds every acknowledgment next to the construct it concerns, using `Validations.of(<construct>).acknowledge(Acknowledgment(id=..., reason=...))` with the exact finding id copied from the synth report). Expected acknowledgments and their reasons:
    - `AwsSolutions-SQS3` and `Serverless-SQSRedrivePolicy` on the DLQ: `This queue is the dead-letter queue of the scheduler; a second-level DLQ adds nothing`.
    - `AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]` on each Lambda role: `Lambda logging baseline: CreateLogGroup, CreateLogStream, PutLogEvents only`.
    - `AwsSolutions-IAM5[Resource::*]` on the detector role: `Account-wide inventory: EC2 Describe* actions do not support resource-level permissions`.
    - `AwsSolutions-IAM5[Resource::<bucket arn>/plans/*]` on the detector and notifier roles: `Object-level grant scoped to the plans/ prefix`.
    - `AwsSolutions-IAM5[Resource::<function arn>:*]` on the state machine role: `CDK LambdaInvoke grants the function and its versions`.
    - `AwsSolutions-L1` and `Serverless-LambdaLatestVersion` on both functions: `Python 3.13 pinned across repo, CI and runtime; upgrade tracked in roadmap`.
    - `Serverless-LambdaDLQ` on both functions: `Invoked synchronously by Step Functions; failures are caught by the state machine and alarmed`.
    - `Serverless-LambdaTracing` on both functions, `AwsSolutions-SF2` and `Serverless-StepFunctionStateMachineXray` on the state machine: `Weekly batch; observability via report, logs and alarms`.
    - `AwsSolutions-SF1` on the state machine: `ERROR-level execution logging is deliberate; the report and the audit table hold the detail`.
    - `AwsSolutions-S1` on the plans bucket: `Append-only versioned bucket; CloudTrail data events are added in Plan 3`.
    - Any further id the strict synth reports: acknowledge it only with a reason that would survive a reviewer, or fix the resource; never acknowledge blindly.
    `make nag` (`cdk synth --strict`) must exit 0.
  - Snapshot test: `Template.from_stack(stack).to_json()` with every `S3Key` matching `[0-9a-f]{64}\.zip` replaced by `ASSET.zip`, compared to `tests/snapshots/LabJanitorStack.template.json`; regenerate with `UPDATE_SNAPSHOTS=1 pytest tests/infra/test_snapshot.py`.
  - Test helper `state_machine_definition(template) -> dict` in `tests/infra/helpers.py`: takes the `AWS::StepFunctions::StateMachine` resource, joins the `DefinitionString["Fn::Join"][1]` list replacing every non-string part with the literal `"X"`, then `json.loads`.

- [ ] **Step 1: Write the failing tests**

```python
def test_choice_takes_report_path_for_string_false(template):
    definition = state_machine_definition(template)
    assert definition["States"]["Defaults"]["Result"]["dryRun"] is True
    choice = definition["States"]["HasCandidates"]
    assert choice["Default"] == "NotifyReportOnly"
    assert "StringEquals" not in json.dumps(choice)
    assert "BooleanEquals" in json.dumps(choice)

def test_schedule_input_and_retries(template):
    template.has_resource_properties("AWS::Scheduler::Schedule", {"ScheduleExpressionTimezone": "America/Bogota", "State": "ENABLED", "Target": Match.object_like({"RetryPolicy": {"MaximumRetryAttempts": 2, "MaximumEventAgeInSeconds": 3600}})})

def test_lambda_invoke_tasks_retry_on_too_many_requests(template): ...
def test_alarms(template):
    template.resource_count_is("AWS::CloudWatch::Alarm", 2)
    template.has_resource_properties("AWS::CloudWatch::Alarm", {"TreatMissingData": "notBreaching", "Threshold": 0})

def test_no_allow_statement_grants_delete_actions(template):
    # Plan 1 guardrail: no role may Delete*/Release*/Terminate* anything
    ...

def test_stack_has_description_and_tags(template): ...
def test_template_snapshot(template): ...
```

- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement `notifier.py`, `workflow.py`, wire everything in `stack.py`, register the cdk-nag packs in `app.py`**
- [ ] **Step 4: Iterate the strict synth until clean**

Run: `CDK_DEFAULT_ACCOUNT=123456789012 CDK_DEFAULT_REGION=us-east-1 npx cdk synth --strict -c reportEmail=ci@example.com --quiet`; for each reported finding, copy its exact id into an acknowledgment next to the construct with the reason from the list above (or fix the resource); repeat until exit 0.

- [ ] **Step 5: Run the full suite**

Run: `make lint && make test && cfn-lint --ignore-checks W3005 -- cdk.out/*.template.json`
Expected: all exit 0.

- [ ] **Step 6: Remove `continue-on-error` from `ci.yml`, generate the snapshot, commit**

```bash
UPDATE_SNAPSHOTS=1 pytest tests/infra/test_snapshot.py -q
git add -A && git commit -m "feat(cdk): weekly workflow with dry-run report path, scheduler, DLQ and alarms"
```

---

### Task 14: Deploy in dry-run and verify a manual run

**Files:**
- Create: `docs/runbook.md` (section `Manual dry-run`, two paragraphs; Plan 3 completes the runbook)
- Modify: `README.md` stub (add the three commands to deploy in dry-run)

**Interfaces:**
- Consumes: the deployed `LabJanitorStack`.

- [ ] **Step 1: Sign in, concurrency check and CloudTrail inventory (spec phase 0)**

Run: `aws sso login && aws sts get-caller-identity && aws lambda get-account-settings --query AccountLimit.UnreservedConcurrentExecutions`
Expected: identity printed; unreserved concurrency at least 101.

Run: `aws cloudtrail describe-trails --include-shadow-trails --region us-east-1` and `aws cloudtrail list-event-data-stores --region us-east-1`
Record in `docs/runbook.md` (section `CloudTrail baseline`) what exists (trail names, multi-region, organization trail, S3 bucket) and the `createTrail` value Plan 3 must use: `dataEventsOnly` when a trail already delivers management events for this account, `full` otherwise.

- [ ] **Step 2: Deploy in dry-run with the owner's addresses passed as context (never committed)**

Run: `npx cdk deploy -c reportEmail=<owner> -c alertEmail=<owner> --require-approval broadening`
Expected: `LabJanitorStack` CREATE_COMPLETE; confirm the two SNS subscription emails.

- [ ] **Step 3: Start a manual execution**

Run: `aws stepfunctions start-execution --state-machine-arn <StateMachineArn output of the stack> --input '{"dryRun": true, "minAgeDays": 7}'` then `describe-execution` until `SUCCEEDED`.
Expected: status `SUCCEEDED`; a report email with subject starting `[Lab Janitor] Weekly report` and ending `(DRY RUN)`; `plans/<runId>/plan.json` and `report.txt` in the bucket; `RUN#<runId>` items in the audit table.

- [ ] **Step 4: Verify the error section is honest**

Expected in the report: an `ERRORS` section listing `internetmonitor` as `service-unavailable-in-region` for the opt-in regions where it does not exist, and no `ACCOUNT CLEAN` line while errors exist.

- [ ] **Step 5: Write the runbook sections (`CloudTrail baseline`, `Manual dry-run`) and README commands, commit**

```bash
git add docs/runbook.md README.md
git commit -m "docs: manual dry-run instructions after first deployment"
```

The old `AwsJanitorStack` keeps running unchanged during Plan 1 (it only reads and, with no lab-tagged resources present, deletes nothing). Plan 3 switches it to dry-run before the controlled deletion test.
