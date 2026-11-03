# Lab Janitor Plan 2: Approval Flow and Executor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the dry-run janitor into the full "plan, explicit human approval, apply" pipeline: an approval request with links (or an IAM-signed call), an executor with its own scoped delete role that applies only the approved plan version after re-verifying every item, and the audit records and EventBridge events for every outcome. Deletions stay disabled in the deployed stack until Plan 3's cutover.

**Architecture:** The state machine gains the approval branch: `NewApprovalId` (a `States.UUID()`), `RequestApproval` (notifier in `approval` mode with `waitForTaskToken`, 72 h), `Apply` (executor), the `Record*`/`Mirror*` DynamoDB states and the `Notify*` outcomes. Two new Lambda packages: `src/approval` (HTTP API handler: GET page, POST decision, POST IAM route) and `src/executor` (never lists, iterates plan items only). A new `shared/approvals.py` owns the approval record and its status transitions; everything else reuses the Plan 1 modules (`plan_store`, `audit`, `settings`, `marking`). CDK adds an HTTP API construct, an executor construct with the double-locked delete policy, an approvals topic that only the notifier may publish to, and moves the outputs to the stack.

**Tech Stack:** Python 3.13, boto3 from the Lambda runtime, AWS CDK v2 (aws-cdk-lib 2.275.0; `aws_apigatewayv2`, `aws_apigatewayv2_integrations`, `aws_apigatewayv2_authorizers`, `aws_stepfunctions_tasks`), pytest + botocore Stubber, ruff, cdk-nag 3.0.2, cfn-lint.

**Spec:** `docs/superpowers/specs/2026-11-02-lab-janitor-portfolio-design.md` v3. Sections implemented here: 4.1 (approvals topic), 4.2 (every state), 4.3, 4.5 (approval family, ELIGIBLE events), 4.6, 4.7 (notifier, approval handler, executor, state machine rows), 4.8 (EIP age rule), 4.9, 5.3 (`approvalEmail`, `allowedRegions`, `approvalAuth`, `approverRoleArn`), 5.4 (the unit and infra gates that name the approval handler and the executor), `scripts/approve.py`. Left for Plan 3: heartbeat and `ApprovalPending` alarms (4.10), CloudTrail (4.5 external evidence), `tests/iam/simulate_policies.sh`, the controlled deletion test (6), enabling deletions. Left for Plan 4: README, ADRs, threat model.

**Plan 1 follow-ups folded in** (from `docs/superpowers/plans/2026-11-03-plan-1-followups.md`): `put_run_summary` takes an `actor`; `allow_plan_writes` is called once (the executor never calls it); stack-level outputs; `Stack.partition` in the plans bucket policy; the janitor's own resources (tag `Project=aws-lab-janitor`) excluded from the inventory; the NotifyFailure `Cause` guard. The remaining Plan 2 items of that file (time budget on the other listings, `read_tags` routing, the long list of small tests) are explicitly deferred to Plan 3 with a note in Task 9.

## Global Constraints

- Everything in the repo in English: code, comments, docstrings, commit messages, email text, HTML copy, resource names.
- Python 3.13 everywhere; Lambda `Runtime.PYTHON_3_13`, arm64.
- No em dashes in AWS resource names or descriptions; hyphens only.
- No account id, email address or secret in any file or commit. Placeholders allowed in tests, CI and snapshots: `example.com` addresses, account ids `123456789012` and `111122223333`.
- `cdk synth` and `pytest` pass with no AWS credentials; `make lint`, `make test`, `make nag` and `cfn-lint --ignore-checks W3005 -- cdk.out/*.template.json` exit 0 at the end of every task that touches CDK or Python.
- Fail safe at every layer: `dryRun` is true unless a boolean `false` says otherwise; the executor deletes nothing unless `DELETIONS_ENABLED` is the string `true` **and** the run config carries boolean `dryRun: false` **and** the plan and approval integrity checks pass; any doubt during re-verification is a SKIPPED, never a delete.
- Append-only audit: every run event and mirror item is a conditional put (`attribute_not_exists(pk) AND attribute_not_exists(sk)`); the only `UpdateItem` calls are the approval `META` status transitions.
- Unit tests use botocore `Stubber` with exact `expected_params`, call `assert_no_pending_responses()` and, for code that goes through `safe`, assert the recorded errors list is empty.
- Marker constants stay in `lab_janitor/markers.py` and reach the Lambdas only as environment variables.
- The old repo at `<old-repo>` is read-only reference. All work happens on `main` of `<repo>`.
- Deploys are run by the owner from their terminal (the agent permission policy blocks `cdk deploy`); the agent verifies with boto3 from `.venv` using `profile_name="<profile>"`, region `us-east-1`. The account's Lambda concurrency quota is 10, so deploys pass `-c reservedConcurrency=false`.

## Review Focus

1. A mail scanner or link preview fetches the approval link: `GET` must change nothing and a `POST` with no form body must return `400` and leave the record `PENDING`. Tests in Task 4.
2. The approve link is clicked after 72 h: the handler returns `410`, sets `EXPIRED`, and never calls `SendTaskSuccess`; the state machine has already taken the timeout branch. Tests in Task 4 (handler) and Task 8 (catch on `States.Timeout`).
3. Between detection and approval a candidate changed (tagged `janitor=keep`, attached, now backs an AMI, or already deleted by hand): the executor writes SKIPPED with the reason and makes no delete call. Tests in Task 5.
4. The stack is deployed with `dryRun` context true and someone starts an execution with `{"dryRun": false}`: the executor's `DELETIONS_ENABLED` is `false`, so every approved item is SKIPPED `deletions-disabled`. Tests in Task 5 (handler) and Task 7 (environment tied to config).
5. Someone replays an approval id, or the task payload names a resource that is not in the plan version that was approved: a second `POST` returns `409`; the executor only ever iterates the plan it read from S3 and raises before touching anything when the sha256 values disagree. Tests in Tasks 4 and 5.

---

### Task 1: Audit client: ELIGIBLE queries, summary actor, approval records

**Files:**
- Modify: `src/shared/audit.py`
- Create: `src/shared/approvals.py`
- Test: `tests/unit/test_audit.py`, `tests/unit/test_approvals.py`

**Interfaces:**
- Consumes: `AuditClient` from Plan 1 (`put_run_event`, `put_run_summary`, `was_proposed_live`, `_put_new`, `_serialize`).
- Produces:
  - `AuditClient.put_run_summary(run_id: str, summary: dict, at: datetime, actor: str = "lambda:detector") -> None` (the `actor` column takes the argument).
  - `AuditClient.was_eligible_live_before(resource_id: str, before: datetime) -> bool`: Query on index `by-resource`, `KeyConditionExpression="resourceId = :id AND #at < :before"`, `FilterExpression="#e = :e AND dryRun = :f"`, names `{"#at": "at", "#e": "event"}`, values `:id` S, `:before` S (ISO of `before` in UTC, same format `_iso` writes), `:e` S `ELIGIBLE`, `:f` BOOL false; pages until a match like `was_proposed_live`. `was_proposed_live` is deleted (Task 2 removes its last caller).
  - Constants in `shared/approvals.py`: `APPROVAL_PREFIX = "APPROVAL#"`, `META_SK = "META"`, statuses `PENDING`, `DECIDING`, `APPROVED`, `REJECTED`, `EXPIRED`, `APPROVAL_TTL_DAYS = 30`.
  - `@dataclass(frozen=True) class ApprovalRecord`: `approval_id: str`, `run_id: str`, `plan_version_id: str`, `plan_sha256: str`, `plan_summary: dict`, `status: str`, `created_at: str`, `expires_at: str`, `approved_by: dict | None`, `decided_at: str | None`; `from_item(item: dict) -> ApprovalRecord` takes a deserialized (plain Python) item.
  - `class ApprovalStore(ddb_client, table_name)`:
    - `create(record: ApprovalRecord, task_token: str) -> None`: conditional put of item `{pk: APPROVAL#<id>, sk: META, approvalId, runId, planVersionId, planSha256, planSummary, taskToken, status, createdAt, expiresAt, ttl}` where `ttl` is the epoch seconds of `expires_at + 30 days`; a duplicate raises `ApprovalExistsError`.
    - `get(approval_id: str) -> tuple[ApprovalRecord, str] | None`: `GetItem` with `ConsistentRead=True`; returns the record and the task token, or None.
    - `claim(approval_id: str, now: str, approver: dict) -> str`: `UpdateItem` `SET #s = :deciding, approvedBy = :by` with `ConditionExpression="#s = :pending AND expiresAt > :now"` and `ReturnValuesOnConditionCheckFailure="ALL_OLD"`; returns `DECIDING`; on `ConditionalCheckFailedException` raises `AlreadyDecidedError` when the old item's status is not `PENDING`, else `ExpiredError`.
    - `revert(approval_id: str) -> None`: `SET #s = :pending REMOVE approvedBy` with condition `#s = :deciding`; a conditional failure is swallowed with a warning.
    - `finalize(approval_id: str, status: str, decided_at: str) -> None`: `SET #s = :status, decidedAt = :at` with condition `#s = :deciding`; a conditional failure raises `AlreadyDecidedError`.
    - `expire(approval_id: str, decided_at: str) -> None`: `SET #s = :expired, decidedAt = :at` with condition `#s IN (:pending, :deciding)`; a conditional failure is swallowed.
    - Exceptions: `ApprovalExistsError`, `AlreadyDecidedError`, `ExpiredError` (all subclass `ApprovalError(Exception)`).
  - `new_approval_id()` is **not** defined here: the state machine generates the id (Task 8). `expiry_for(created_at: datetime) -> datetime` returns `created_at + 71 h 30 min`; `APPROVAL_WINDOW = timedelta(hours=71, minutes=30)`.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_audit.py` (reuse `ddb_stub`, `audit`, `event_item` fixtures):

```python
def test_put_run_summary_takes_an_actor(ddb_stub):
    # expected Item has "actor": {"S": "lambda:executor"} and sk RUN#SUMMARY
    ...
    audit(ddb_stub).put_run_summary(RUN_ID, {"deleted": 1}, AT, actor="lambda:executor")

def test_was_eligible_live_before_queries_by_resource_and_time(ddb_stub):
    ddb_stub.add_response("query", {"Items": [{"pk": {"S": "RUN#x"}}], "Count": 1}, {
        "TableName": TABLE, "IndexName": "by-resource",
        "KeyConditionExpression": "resourceId = :id AND #at < :before",
        "FilterExpression": "#e = :e AND dryRun = :f",
        "ExpressionAttributeNames": {"#at": "at", "#e": "event"},
        "ExpressionAttributeValues": {":id": {"S": "eipalloc-1"}, ":before": {"S": "2026-10-26T12:00:05+00:00"},
                                      ":e": {"S": "ELIGIBLE"}, ":f": {"BOOL": False}}})
    assert audit(ddb_stub).was_eligible_live_before("eipalloc-1", AT - timedelta(days=7)) is True

def test_was_eligible_live_before_is_false_when_pages_are_exhausted(ddb_stub): ...  # two pages, no items
def test_was_proposed_live_is_gone():
    assert not hasattr(AuditClient, "was_proposed_live")
```

In `tests/unit/test_approvals.py` (new; `ddb_stub` via `stub_factory("dynamodb")`):

```python
RECORD = ApprovalRecord(approval_id="a1b2", run_id="20261103T120000Z-7f3a", plan_version_id="v1",
    plan_sha256="ab" * 32, plan_summary={"candidateCount": 1}, status="PENDING",
    created_at="2026-11-03T12:00:00+00:00", expires_at="2026-11-06T11:30:00+00:00",
    approved_by=None, decided_at=None)

def test_expiry_is_71_hours_30_minutes_after_creation(): ...
def test_create_writes_conditional_put_with_ttl(ddb_stub):
    # Item includes taskToken {"S": "tok"}, ttl {"N": str(epoch(expires_at) + 30 days)}, ConditionExpression "attribute_not_exists(pk) AND attribute_not_exists(sk)"
def test_create_raises_when_record_exists(ddb_stub): ...  # ConditionalCheckFailedException -> ApprovalExistsError
def test_get_returns_record_and_token(ddb_stub): ...      # ConsistentRead True; from_item round-trip
def test_get_returns_none_when_missing(ddb_stub): ...
def test_claim_sets_deciding_with_pending_and_unexpired_condition(ddb_stub): ...
def test_claim_raises_already_decided_when_old_status_is_not_pending(ddb_stub): ...  # error response carries Item with status APPROVED
def test_claim_raises_expired_when_old_status_is_pending(ddb_stub): ...
def test_revert_swallows_conditional_failure(ddb_stub): ...
def test_finalize_raises_already_decided_on_conditional_failure(ddb_stub): ...
def test_expire_accepts_pending_or_deciding(ddb_stub): ...  # ConditionExpression "#s IN (:pending, :deciding)"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd <repo> && .venv/bin/pytest tests/unit/test_audit.py tests/unit/test_approvals.py -q`
Expected: failures on the new names (`ImportError` for `shared.approvals`, `TypeError` on `actor`, `AttributeError` on `was_eligible_live_before`).

- [ ] **Step 3: Implement `audit.py` changes and `approvals.py`**

`ApprovalStore` serializes with `TypeSerializer` like `AuditClient` (import `_serialize` or move it to a tiny `shared/dynamo.py` with `serialize(item)` and `deserialize(item)` using `TypeDeserializer`; the latter is cleaner, do that and update `audit.py` to import it). `ReturnValuesOnConditionCheckFailure` is a parameter of `UpdateItem`; read the old item from `error.response["Item"]` and deserialize it to tell `AlreadyDecidedError` from `ExpiredError`.

- [ ] **Step 4: Run the tests and the suite**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check . && .venv/bin/ruff format --check .`
Expected: all pass (the detector handler tests still pass because `put_run_summary` keeps its default actor).

- [ ] **Step 5: Commit**

```bash
git add src/shared tests/unit/test_audit.py tests/unit/test_approvals.py
git commit -m "feat(shared): approval records and ELIGIBLE lookups in the audit client"
```

---

### Task 2: Detector: EIP age rule with ELIGIBLE events, own-resource exclusion

**Files:**
- Modify: `src/shared/findings.py`, `src/shared/marking.py`, `src/detector/sweep.py`, `src/detector/detectors/ec2.py`, `src/detector/detectors/common.py`, `src/detector/handler.py`
- Test: `tests/unit/test_detect_ec2.py`, `tests/unit/test_detector_handler.py`, `tests/unit/conftest.py`, `tests/unit/test_plan.py`, `tests/unit/test_marking.py`

**Interfaces:**
- Consumes: `AuditClient.was_eligible_live_before` (Task 1).
- Produces:
  - `Finding.eligible: bool = False` (dataclass field after `candidate`), included in `to_dict()` as `"eligible"`.
  - `SweepContext.was_eligible_before: Callable[[str], bool]` replaces `was_proposed_live`. The handler binds it as `lambda rid: audit.was_eligible_live_before(rid, ctx.now - timedelta(days=settings.min_age_days))`; `main()` binds `lambda _rid: False`.
  - `_address` verdict: `eligible = is_lab_tagged and not associated and allocation_id is not None and first_blocker(tags, settings) is None`; `candidate = eligible and ctx.was_eligible_before(allocation_id)`; reasons: candidate `"idle, tagged <lab tag>, first seen idle in a live run at least <minAgeDays> days ago"`; eligible but not candidate `"idle, not yet <minAgeDays> days since the first live run that saw it idle"`; associated `"associated"`; kept `"tagged janitor=keep"`.
  - Handler audit loop writes `("DETECTED", detected)`, `("PROPOSED", candidates)` and `("ELIGIBLE", [item for item in detected if item["eligible"]])`, in that order.
  - `common.make_finding` passes `eligible` through `fields` (default False).
  - Own-resource exclusion (Plan 1 follow-up): `shared/marking.py` gains `is_janitor_owned(tags: list[dict] | None) -> bool` (`Project` tag equals `aws-lab-janitor`, constant `JANITOR_PROJECT_TAG = ("Project", "aws-lab-janitor")`). The filter lives in one place: `detector.handler.detect_plan` drops findings whose `tags` say janitor-owned before `build_plan`, logging the count at INFO. Detectors are untouched.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_detect_ec2.py: replace test_eip_requires_previous_live_proposal
def test_eip_age_rule(ec2_stub, ctx_factory):
    idle = {"describe_addresses": {"Addresses": [_address("eipalloc-1")]}}
    stub_ec2(ec2_stub, responses=idle); stub_ec2(ec2_stub, responses=idle)
    first, _ = run(ctx_factory(was_eligible_before=lambda rid: False))
    eip = by_id(first, "elastic-ip")["eipalloc-1"]
    assert eip.eligible is True and eip.candidate is False
    assert eip.reason == "idle, not yet 7 days since the first live run that saw it idle"
    second, _ = run(ctx_factory(was_eligible_before=lambda rid: True))
    eip = by_id(second, "elastic-ip")["eipalloc-1"]
    assert eip.candidate is True and eip.eligible is True
    assert eip.reason == "idle, tagged Purpose=lab, first seen idle in a live run at least 7 days ago"

def test_associated_or_kept_eip_is_not_eligible(ec2_stub, ctx_factory): ...  # eligible False for both; reason "associated" / "tagged janitor=keep"
def test_legacy_address_without_allocation_id_is_not_eligible(...): ...
# tests/unit/test_marking.py
def test_is_janitor_owned(): ...  # Project=aws-lab-janitor -> True; other value, missing, None -> False

# tests/unit/test_detector_handler.py
def test_handler_writes_eligible_events_for_eligible_findings(...):  # events contain ("eipalloc-1", "ELIGIBLE") with dry_run from settings; order DETECTED, PROPOSED, ELIGIBLE
def test_handler_binds_eligible_lookup_with_min_age(...):  # FakeAudit.was_eligible_live_before records (resource_id, before); before == ctx.now - 7 days
def test_main_never_consults_the_audit_table(...):  # existing test adapted: was_eligible_before returns False
def test_detect_plan_drops_janitor_owned_findings(...):  # a finding tagged Project=aws-lab-janitor is absent from candidates and inventory

# tests/unit/test_plan.py
def test_candidate_dict_carries_eligible(): ...
```

Update `tests/unit/conftest.py` (`ctx_factory` default key `was_eligible_before`) and the handler `FakeAudit` (`was_eligible_live_before(self, resource_id, before)` recording calls, returning False).

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_detect_ec2.py tests/unit/test_detector_handler.py tests/unit/test_plan.py -q`

- [ ] **Step 3: Implement**

`_address` keeps a single return; compute `eligible` and `candidate` with `first_blocker` as today. In the handler, build the ELIGIBLE list from `detected` so a candidate EIP gets both PROPOSED and ELIGIBLE (the ELIGIBLE record is what the next run's age query reads; a PROPOSED record does not count).

- [ ] **Step 4: Run the whole suite and lint**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check . && .venv/bin/ruff format --check .`
Expected: pass. The infra snapshot does not change (no CDK change in this task).

- [ ] **Step 5: Commit**

```bash
git add src tests
git commit -m "feat(detector): EIP age rule from ELIGIBLE audit records; skip the janitor's own resources"
```

---

### Task 3: Plan summary and notifier modes: approval, result, expired, rejected

**Files:**
- Create: `src/shared/plan_summary.py`, `src/notifier/approval.py`, `src/notifier/outcomes.py`
- Modify: `src/notifier/handler.py`
- Test: `tests/unit/test_plan_summary.py`, `tests/unit/test_notifier_approval.py`, `tests/unit/test_notifier_outcomes.py`, `tests/unit/test_notifier_handler.py`

**Interfaces:**
- Consumes: `PlanStore.get_plan(run_id, version_id) -> (plan, sha256)`, `PlanStore.get_report`, `ApprovalStore.create`, `ApprovalRecord`, `expiry_for` (Task 1), `truncate_report`, `report_subject` pattern from Plan 1.
- Produces:
  - `summarize_plan(plan: dict, limit: int = 50) -> dict`: `{"runId", "generatedAt", "candidateCount", "estimatedMonthlyUsd" (sum of candidates' known costs, rounded to 2), "errorCount", "regions": sorted distinct candidate regions, "candidates": [{"region","kind","resourceId","reason","estimatedMonthlyUsd"} for the first `limit` candidates in plan order], "truncated": bool}`. Bounded by construction (`limit` 50 keeps the DynamoDB item far below 400 KB).
  - `render_summary(summary: dict) -> str`: text block, one line per candidate `[kind] resourceId (region) reason ~$X/mo`, plus a final `... and N more` line when truncated.
  - Notifier environment: `PLANS_BUCKET`, `REPORTS_TOPIC_ARN` (existing), `APPROVALS_TOPIC_ARN`, `AUDIT_TABLE`, `APPROVAL_URL` (HTTP API base URL, no trailing slash), `APPROVAL_AUTH` (`link` | `iam` | `both`).
  - `notifier.handler.handler(event, context)` dispatches on `event["mode"]`: `report` (unchanged), `approval` → `approval.request_approval(event)`, `result` → `outcomes.publish_result(event)`, `expired` → `outcomes.publish_expired(event)`, `rejected` → `outcomes.publish_rejected(event)`; unknown mode raises `ValueError`.
  - `request_approval(event) -> dict` with `event = {"mode": "approval", "taskToken": str, "approvalId": str, "detect": {...}, "config": {...}}`:
    1. `plan, sha = store.get_plan(runId, planVersionId)`; if `sha != detect["planSha256"]` raise `PlanIntegrityError` (no record, no email).
    2. `summary = summarize_plan(plan)`; `created = datetime.now(UTC)`; record with `status PENDING`, `expires_at = expiry_for(created)`; `approvals.create(record, task_token)`.
    3. Publish to `APPROVALS_TOPIC_ARN`: subject `[Lab Janitor] Approval needed: N deletions, ~$X.XX/month, run <runId>` (fallback `[Lab Janitor] Approval needed` when 100 chars or more); message built by `approval_message(record, summary, approval_url, auth, confirmation)` where `confirmation = runId[-4:]`:
       - `link` or `both`: lines `Review and decide: <APPROVAL_URL>/approvals/<id>`, `Type the confirmation code <last4> on that page.`, `This request expires at <expiresAt> (UTC).`
       - `iam` or `both`: lines `Approve with your AWS identity:` and `python scripts/approve.py --url <APPROVAL_URL> <id> approve` (and the `reject` variant).
       - always: `Plan sha256: <sha>`, the rendered summary, `Full plan: s3://<bucket>/plans/<runId>/plan.json`.
    4. Publish the weekly report to `REPORTS_TOPIC_ARN` exactly as `report` mode does (reuse `publish_report`), subject without the DRY RUN marker.
    5. Return `{"approvalId": id, "expiresAt": ..., "messageId": ...}` (ignored by Step Functions in `waitForTaskToken` mode; useful in logs).
  - `publish_result(event)` with `event["apply"] = {"deleted", "failed", "skipped", "items": [...]}` and `event["approval"]["approvedBy"]`: subject `[Lab Janitor] Deletions applied: D deleted, F failed, S skipped` (fallback `[Lab Janitor] Deletions applied`); message lists each item `OUTCOME [kind] resourceId (region): reason`, then `Approved by: <mode> <principal or ip> <userAgent> at <at>`, then the plan pointer. `publish_expired(event)` subject `[Lab Janitor] Approval expired: run <runId>`; `publish_rejected(event)` subject `[Lab Janitor] Approval rejected: run <runId>` with the rejecter line when `event["error"]["Cause"]` parses as JSON with `rejectedBy`. All three publish to `REPORTS_TOPIC_ARN`.
  - `build_dependencies()` returns `(store, sns, approvals)`; approvals is an `ApprovalStore` over `boto3.client("dynamodb")`.

- [ ] **Step 1: Write the failing tests**

```python
# test_plan_summary.py
def test_summary_counts_costs_and_regions(): ...                 # two candidates, one without cost -> estimatedMonthlyUsd sums the known one
def test_summary_truncates_at_limit_in_plan_order(): ...         # limit=2 on three candidates -> truncated True, 2 items
def test_render_summary_lines(): ...                             # exact line format and "... and 1 more"

# test_notifier_approval.py (Stubber on s3 + sns + dynamodb; monkeypatch env, freeze time via a `now` parameter on request_approval)
def test_request_approval_writes_record_and_publishes_links(): ...   # APPROVAL_AUTH=link: message contains "/approvals/<id>" and the last 4 chars; no "approve.py"
def test_request_approval_iam_mode_sends_cli_command_not_links(): ...
def test_request_approval_both_mode_sends_both(): ...
def test_request_approval_refuses_sha_mismatch(): ...               # PlanIntegrityError; no put_item, no publish (stubber has no pending responses)
def test_request_approval_subject_falls_back_when_too_long(): ...
def test_request_approval_also_publishes_the_report(): ...          # two sns publishes: approvals then reports

# test_notifier_outcomes.py
def test_result_message_lists_items_and_approver(): ...
def test_expired_and_rejected_subjects(): ...
def test_rejected_includes_rejecter_when_cause_is_json(): ...

# test_notifier_handler.py
def test_handler_dispatches_every_mode(monkeypatch): ...            # each mode calls its function once
```

- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement the three modules**

`request_approval(event, now: datetime | None = None)`; the handler passes nothing. Keep `report` mode code where it is; import `publish_report` from `notifier.handler` inside `approval.py` lazily or move `publish_report` into `src/notifier/report.py` and have the handler import it (preferred: `handler.py` becomes dispatch only).

- [ ] **Step 4: Run suite and lint**
- [ ] **Step 5: Commit**

```bash
git add src/shared/plan_summary.py src/notifier tests/unit
git commit -m "feat(notifier): approval request, result, expired and rejected emails"
```

---

### Task 4: Approval handler Lambda (HTTP API)

**Files:**
- Create: `src/approval/__init__.py`, `src/approval/handler.py`, `src/approval/page.py`
- Test: `tests/unit/test_approval_page.py`, `tests/unit/test_approval_handler.py`

**Interfaces:**
- Consumes: `ApprovalStore` (`get`, `claim`, `revert`, `finalize`, `expire`), `render_summary` (Task 3).
- Produces:
  - Environment: `AUDIT_TABLE`, `APPROVAL_AUTH`, `APPROVER_ROLE_ARN` (empty string when unset).
  - `handler(event, context) -> dict` for HTTP API payload format 2.0. Routes by `event["routeKey"]`:
    - `GET /approvals/{id}`: `200` HTML page (`page.render_form(record, summary_text)`), or `404` page when the id is unknown, or `410` page when `status == EXPIRED` or `expiresAt <= now`, or `409` page when already decided. **No writes.**
    - `POST /approvals/{id}` (form-encoded body, base64 when `isBase64Encoded`): fields `decision` (`approve` | `reject`) and `confirm`. Sequence: parse body (missing or malformed → `400` **before** any write); `claim(id, now, approver)` where `approver = {"mode": "link", "ip": requestContext.http.sourceIp, "userAgent": requestContext.http.userAgent, "at": now}`; `AlreadyDecidedError` → `409`, `ExpiredError` → `410`, unknown id → `404`; wrong `confirm` (not equal to `record.run_id[-4:]`) → `revert`, `400`; then `states.send_task_success(taskToken=token, output=json.dumps({"decision": "approved", "approvalId": id, "approvedBy": approver}))` or `states.send_task_failure(taskToken=token, error="Rejected", cause=json.dumps({"approvalId": id, "rejectedBy": approver}))`; on `TaskTimedOut` → `expire`, `410`; on any other `ClientError` → `revert`, `500`; then `finalize(id, APPROVED | REJECTED, now)` → `200` page `page.render_done(decision)`.
    - `POST /iam/approvals/{id}` (JSON body `{"decision": "approve" | "reject"}`): `principal = requestContext.authorizer.iam.userArn`; `allowed_principal(principal, APPROVER_ROLE_ARN) -> bool` is true when the role name of `APPROVER_ROLE_ARN` (`arn:...:role/<name>` or `.../role/aws-reserved/sso.amazonaws.com/<region>/<name>`) equals the role name in an `assumed-role/<name>/<session>` ARN, or when the two ARNs are equal; otherwise `403` JSON **before** any write. Same claim / callback / finalize sequence with `approver = {"mode": "iam", "principal": principal, "at": now}`, no typed confirmation. Responses are JSON (`{"status": "APPROVED"}` etc.).
    - Any other route → `404`.
  - Every response carries `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; form-action 'self'`, and for HTML `Content-Type: text/html; charset=utf-8`.
  - `page.render_form(record, summary_text, confirmation_hint="last 4 characters of the run id") -> str`: static HTML, no `<script>`, no external URL, one `<form method="post">` with a `confirm` text input and two submit buttons `name="decision" value="approve"` / `value="reject"`; shows run id, sha256, expiry and the summary inside `<pre>` with HTML escaping (`html.escape`). `render_status(title, text)` for 404/409/410/500 and `render_done(decision)`.
  - Logging: `logging.getLogger("approval")`; every log line carries `approvalIdHash = sha256(id)[:16]`, never the id or the token.
  - `build_dependencies() -> (ApprovalStore, states_client)`.

- [ ] **Step 1: Write the failing tests**

```python
# test_approval_page.py
def test_form_has_no_script_and_escapes_summary(): ...    # "<script" absent; "&lt;b&gt;" present for a summary containing "<b>"
def test_form_posts_to_self_with_confirm_field(): ...

# test_approval_handler.py (Stubber on dynamodb and stepfunctions; helper `http_event(method, route, path_id, body=None, iam_arn=None)` building payload v2)
def test_get_renders_form_and_writes_nothing(): ...        # only a get_item response stubbed
def test_get_unknown_id_is_404(): ...
def test_get_expired_record_is_410(): ...
def test_post_without_body_is_400_and_writes_nothing(): ...
def test_post_approve_happy_path(): ...                     # update(claim) -> send_task_success with exact output -> update(finalize APPROVED); 200
def test_post_reject_sends_task_failure_with_cause(): ...
def test_post_wrong_confirmation_reverts_and_400(): ...
def test_second_post_is_409(): ...                          # claim raises AlreadyDecidedError (old status APPROVED)
def test_post_after_expiry_is_410(): ...
def test_post_task_timed_out_marks_expired_and_410(): ...   # send_task_success raises TaskTimedOut -> expire -> 410
def test_post_other_states_error_reverts_and_500(): ...
def test_iam_route_rejects_other_principals_before_any_write(): ...   # 403; no stubbed responses consumed
def test_iam_route_accepts_sso_permission_set_role(): ...              # APPROVER_ROLE_ARN arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/us-east-1/AWSReservedSSO_Admin_abc, caller arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_Admin_abc/user -> 200, approver mode iam with principal
def test_iam_route_is_404_when_auth_mode_is_link(): ...
def test_link_routes_are_404_when_auth_mode_is_iam(): ...
def test_responses_carry_hardening_headers(): ...
def test_logs_never_contain_the_approval_id(caplog): ...
```

- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement `handler.py` and `page.py`**

Parse form bodies with `urllib.parse.parse_qs`; `now` is `datetime.now(UTC).isoformat()`. `TaskTimedOut` arrives as a `ClientError` with code `TaskTimedOut`. The `routeKey` strings are exactly `GET /approvals/{id}`, `POST /approvals/{id}`, `POST /iam/approvals/{id}`; the id is `event["pathParameters"]["id"]`. Mode gating: link routes exist only when `APPROVAL_AUTH in (link, both)`; the IAM route only when in `(iam, both)`; the handler returns `404` for a route its mode does not serve even if the API exposes it.

- [ ] **Step 4: Run suite and lint**
- [ ] **Step 5: Commit**

```bash
git add src/approval tests/unit/test_approval_page.py tests/unit/test_approval_handler.py
git commit -m "feat(approval): HTTP handler with read-only page, claim-then-callback decision and IAM route"
```

---

### Task 5: Executor Lambda

**Files:**
- Create: `src/executor/__init__.py`, `src/executor/handler.py`, `src/executor/verify.py`, `src/executor/deleters.py`
- Modify: `src/shared/settings.py` (add `deletions_enabled`)
- Test: `tests/unit/test_settings.py`, `tests/unit/test_executor_verify.py`, `tests/unit/test_executor_deleters.py`, `tests/unit/test_executor_handler.py`

**Interfaces:**
- Consumes: `load_settings`, `ClientFactory`, `PlanStore.get_plan`, `AuditClient.put_run_event` / `put_run_summary(actor=...)`, `ApprovalStore.get`, `is_lab_tagged`, `is_kept`, `age_days`, `marking.tag_value`.
- Produces:
  - `Settings.deletions_enabled: bool` = `env.get("DELETIONS_ENABLED") == "true"` (exact lowercase string; anything else is False).
  - Constants in `executor/handler.py`: `ACTOR = "lambda:executor"`, `RESERVE_SECONDS = 30`, `EVENT_SOURCE = "lab-janitor"`, `DETAIL_TYPE = "janitor.resource.{outcome}"` (`deleted`, `failed`, `skipped`), `MAX_RESULT_ITEMS = 200`, `DELETABLE_KINDS = ("ebs-snapshot", "ebs-volume", "elastic-ip", "cw-dashboard", "internet-monitor")`.
  - `handler(event, context) -> dict` with `event = {"detect": {...}, "config": {...}, "approval": {...}, "approvalId": str}`:
    1. `settings = load_settings(event, env)`; `armed = settings.deletions_enabled and settings.dry_run is False and event["approval"].get("decision") == "approved"`.
    2. `plan, sha = store.get_plan(runId, planVersionId)`; `record, _ = approvals.get(approvalId)`; raise `PlanIntegrityError` (and write nothing) unless `sha == detect["planSha256"] == record.plan_sha256`, `record.run_id == runId`, `record.status == "APPROVED"`, `record.approval_id == approvalId`.
    3. For each `item in plan["candidates"]` in plan order: if remaining time (`context.get_remaining_time_in_millis()/1000`) `< RESERVE_SECONDS` → SKIPPED `time-budget`; elif not `armed` → SKIPPED `deletions-disabled`; elif `kind not in DELETABLE_KINDS` → SKIPPED `no-deleter`; else `reason = verify.recheck(item, clients, settings, now)` (None when still deletable) → SKIPPED `reason` when not None, else `deleters.delete(item, clients)` → DELETED, or FAILED `<AWS error code>` on `ClientError` (other exceptions propagate after the audit write of FAILED `exception`).
    4. After each item: `audit.put_run_event(run_id, region, resourceId, kind, outcome, ACTOR, dry_run=False, detail={"reason", "approvalId", "planSha256", "estimatedMonthlyUsd"}, at=now)`, then `events.put_events(Entries=[{"Source": EVENT_SOURCE, "DetailType": DETAIL_TYPE.format(outcome=outcome.lower()), "Detail": json.dumps(record), "EventBusName": "default"}])`; log `before` and `after` lines with runId, resourceId, outcome.
    5. No summary record: the detector owns `RUN#SUMMARY`, and the state machine's `RecordApplied` mirror carries the counts. Return `{"runId", "approvalId", "deleted": int, "failed": int, "skipped": int, "items": first MAX_RESULT_ITEMS of [{"resourceId","kind","region","outcome","reason"}]}`.
  - `verify.recheck(item: dict, clients, settings, now: datetime) -> str | None`, per kind (every call through the regional client `clients.get(service, item["region"])`; global kinds use `us-east-1`):
    - `ebs-snapshot`: `describe_snapshots(SnapshotIds=[id])` → `InvalidSnapshot.NotFound` → `already-gone`; tags not lab → `not-lab-tagged`; kept → `tagged janitor=keep`; `State != completed` → `state <s>`; `age_days(StartTime) < settings.min_age_days` → `below minimum age (<n> days)`; `describe_images(Owners=["self"], IncludeDisabled=True, Filters=[{"Name": "block-device-mapping.snapshot-id", "Values": [id]}])` non-empty → `backs AMI <ImageId>`.
    - `ebs-volume`: `describe_volumes(VolumeIds=[id])` → `InvalidVolume.NotFound` → `already-gone`; tags; kept; `Attachments` non-empty → `attached`; `State != available` → `state <s>`; age.
    - `elastic-ip`: `describe_addresses(AllocationIds=[id])` → `InvalidAllocationID.NotFound` → `already-gone`; tags; kept; `AssociationId` present → `associated`.
    - `cw-dashboard`: name must start with `settings.name_prefix` else `name prefix mismatch`; `list_tags_for_resource(ResourceARN=item["arn"])` (`ResourceNotFoundException` → `already-gone`); kept → `tagged janitor=keep`.
    - `internet-monitor`: name prefix; `get_monitor(MonitorName=name)` (`ResourceNotFoundException` → `already-gone`); `Tags` map kept → `tagged janitor=keep`.
    - A `ClientError` the kind does not expect propagates (the handler records FAILED with its code).
  - `deleters.delete(item: dict, clients) -> None`: `delete_snapshot(SnapshotId)`, `delete_volume(VolumeId)`, `release_address(AllocationId)`, `delete_dashboards(DashboardNames=[name])`, monitor: `get_monitor` status; if not `INACTIVE` → `update_monitor(MonitorName, Status="INACTIVE")` then poll `get_monitor` every `MONITOR_POLL_SECONDS = 5` up to `MONITOR_WAIT_SECONDS = 60` (injectable `sleep`) until `INACTIVE`, else raise `MonitorStillActive` (handler records FAILED `monitor-still-active`); then `delete_monitor(MonitorName)`.
  - No `__main__` block in the executor (spec 4.8, last bullet).

- [ ] **Step 1: Write the failing tests**

```python
# test_settings.py
def test_deletions_enabled_requires_exact_true(): ...       # "true" -> True; "True", "1", "" , missing -> False

# test_executor_verify.py (ec2/cloudwatch/internetmonitor stubs via stub_factory)
def test_snapshot_still_deletable_returns_none(): ...        # describe_snapshots + describe_images(empty) with exact params incl. IncludeDisabled True
def test_snapshot_now_backs_an_ami(): ...
def test_snapshot_gone_is_already_gone(): ...
def test_snapshot_kept_or_untagged_or_young_or_pending(): ...  # parametrized reasons
def test_volume_attached_or_not_available(): ...
def test_eip_associated(): ...
def test_dashboard_prefix_and_keep_tag(): ...
def test_monitor_keep_tag_from_get_monitor(): ...

# test_executor_deleters.py
def test_each_kind_calls_its_delete_api(): ...               # parametrized exact params
def test_monitor_is_disabled_and_polled_before_delete(): ... # get_monitor ACTIVE -> update_monitor -> get_monitor ACTIVE -> get_monitor INACTIVE -> delete_monitor; fake sleep records [5, 5]
def test_monitor_still_active_after_60s_raises(): ...

# test_executor_handler.py (fake store/approvals/audit/events like the detector harness; real Stubber only in verify/deleters tests)
def test_everything_skipped_when_deletions_disabled(): ...   # DELETIONS_ENABLED unset; reasons deletions-disabled; no client calls
def test_everything_skipped_when_dry_run_not_boolean_false(): ...   # config {"dryRun": "false"}
def test_integrity_mismatch_raises_before_any_write(): ...   # sha differs -> PlanIntegrityError; audit.events == []
def test_approval_not_approved_raises(): ...
def test_deletes_only_plan_items_and_audits_each(): ...      # two candidates: one DELETED, one SKIPPED (verify returns reason); audit events + put_events per item; detail carries approvalId and planSha256
def test_failed_on_invalid_snapshot_in_use(): ...            # ClientError InvalidSnapshot.InUse -> FAILED, reason is the code
def test_time_budget_skips_the_rest(): ...                   # context remaining 20 s -> all SKIPPED time-budget
def test_no_delete_call_for_a_resource_absent_from_the_plan(): ...  # event["detect"] mentions nothing extra; a candidate list in event is ignored: executor reads the plan from the store only
def test_result_items_are_bounded(): ...                     # 250 candidates -> items length 200, counts still 250
def test_executor_has_no_main(): ...                         # module has no __main__ guard: "__main__" not in source
```

- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement `settings.py` field, `verify.py`, `deleters.py`, `handler.py`**

The handler builds `ClientFactory()` once; `verify` and `deleters` take the factory and look clients up per item region. Order of checks is the order listed above; the first blocker wins. Use `functools.partial`/a small dispatch dict keyed by kind for both modules.

- [ ] **Step 4: Run suite and lint**
- [ ] **Step 5: Commit**

```bash
git add src/executor src/shared/settings.py tests/unit
git commit -m "feat(executor): apply the approved plan version with per-item re-verification and audit events"
```

---

### Task 6: CDK config, approvals topic, audit grants, notifier construct

**Files:**
- Modify: `lab_janitor/config.py`, `lab_janitor/notifications.py`, `lab_janitor/audit.py`, `lab_janitor/notifier.py`, `lab_janitor/stack.py` (pass the new pieces; outputs move in Task 8)
- Test: `tests/infra/test_config.py`, `tests/infra/test_notifications.py`, `tests/infra/test_audit_store.py`, `tests/infra/test_notifier.py`

**Interfaces:**
- Produces:
  - `JanitorConfig` new fields: `approval_email: str` (context `approvalEmail`, default `report_email`), `allowed_regions: tuple[str, ...]` (context `allowedRegions`: comma-separated string or list; empty tuple means all), `approval_auth: str` (context `approvalAuth` in `("link", "iam", "both")`, default `link`, anything else `ValueError`), `approver_role_arn: str | None` (context `approverRoleArn`; required when `approval_auth != "link"`, must match `^arn:aws[a-z-]*:iam::\d{12}:role/.+$`, else `ValueError` with the key in the message).
  - `Notifications.approvals_topic` (`display_name="Lab Janitor Approvals"`, `enforce_ssl=True`), email subscription construct id `ApprovalsEmail` to `config.approval_email` with the shared DLQ. Topic policy statements: (a) Deny `sns:Subscribe`, `sns:SetTopicAttributes`, `sns:AddPermission`, `sns:RemovePermission` for `Principal: "*"` with `StringNotEquals aws:PrincipalArn` = [`Fn.sub(stack.synthesizer.cloud_formation_execution_role_arn)`, `arn:<partition>:iam::<account>:root`] (raise `RuntimeError("approvals topic needs the DefaultStackSynthesizer")` when the synthesizer has no execution role ARN); (b) Deny `sns:Publish` for `Principal: "*"` with `StringNotEquals aws:PrincipalArn` = notifier role ARN. Because (b) needs the notifier role, `Notifications` exposes `restrict_publishers(role_arn: str) -> None`, called exactly once by the stack after the notifier exists (same pattern as `allow_plan_writes`).
  - `AuditStore` new grants: `grant_approval_writes(grantee, *, actions=("dynamodb:PutItem",))` with `LeadingKeys ["APPROVAL#*"]`; `grant_approval_reads(grantee)` = `dynamodb:GetItem` with the same condition; `grant_approval_updates(grantee)` = `dynamodb:UpdateItem` same condition; `grant_plan_reads(grantee)` = `s3:GetObject`, `s3:GetObjectVersion` on `plans/*`. `Stack.partition` replaces the hardcoded `arn:aws` in `root_arn`.
  - `Notifier` construct: environment adds `APPROVALS_TOPIC_ARN`, `AUDIT_TABLE`, `APPROVAL_AUTH`; `APPROVAL_URL` is set by the stack after the API exists via `self.function.add_environment("APPROVAL_URL", api.url_without_trailing_slash)` (Task 7 provides the API); grants: `approvals_topic.grant_publish`, `audit.grant_approval_writes` (PutItem only); plan reads via `audit.grant_plan_reads` (replacing the inline statement).

- [ ] **Step 1: Write the failing tests**

```python
# test_config.py
@pytest.mark.parametrize("value", ["link", "iam", "both"])
def test_approval_auth_accepts_the_three_modes(value): ...
def test_approval_auth_rejects_other_values(): ...
def test_iam_modes_require_a_valid_approver_role_arn(): ...        # missing -> ValueError mentioning approverRoleArn; "not-an-arn" -> ValueError
def test_allowed_regions_from_comma_string_and_list(): ...          # "us-east-1, eu-west-1" -> ("us-east-1", "eu-west-1"); [] -> ()
def test_approval_email_defaults_to_report_email(): ...

# test_notifications.py
def test_three_topics_and_three_subscriptions(template): ...        # DisplayName Lab Janitor Approvals; subscription endpoint from context approvalEmail
def test_approvals_topic_denies_subscribe_except_cfn_exec_role_and_root(template): ...   # Deny statement with the four actions; condition aws:PrincipalArn list of two
def test_approvals_topic_denies_publish_except_notifier_role(template): ...
def test_no_subscription_leaks_the_address_into_logical_ids(template): ...  # existing pattern, extended

# test_audit_store.py
def test_approval_grants_carry_leading_keys(template): ...          # for notifier (PutItem) condition APPROVAL#*
def test_root_arn_uses_the_partition(template): ...                 # Fn::Join with {"Ref": "AWS::Partition"}

# test_notifier.py
def test_notifier_environment_and_grants(template): ...             # env keys; sns:Publish on reports and approvals; no s3:PutObject
```

- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement**

`Fn.sub` is `aws_cdk.Fn.sub`. `stack.synthesizer` is typed `IStackSynthesizer`; check `isinstance(stack.synthesizer, DefaultStackSynthesizer)` and read `cloud_formation_execution_role_arn`. Update `tests/infra/conftest.py` fixtures if a context key is now required (it is not: `link` needs nothing new).

- [ ] **Step 4: Run suite, lint, strict synth**

Run: `make lint && make test && make nag && .venv/bin/cfn-lint --ignore-checks W3005 -- cdk.out/*.template.json`
Expected: pass (acknowledge any new cdk-nag finding next to its construct with a true reason; the snapshot test will fail until Task 8 regenerates it, so run `UPDATE_SNAPSHOTS=1 .venv/bin/pytest tests/infra/test_snapshot.py -q` here and review the diff).

- [ ] **Step 5: Commit**

```bash
git add lab_janitor tests/infra tests/snapshots
git commit -m "feat(cdk): approvals topic, approval grants and notifier wiring"
```

---

### Task 7: CDK approval API and executor constructs with the delete guardrails

**Files:**
- Create: `lab_janitor/approval_api.py`, `lab_janitor/executor.py`
- Modify: `lab_janitor/stack.py`, `lab_janitor/nag.py`
- Test: `tests/infra/test_approval_api.py`, `tests/infra/test_executor.py`, `tests/infra/test_iam_guardrails.py`, `tests/infra/helpers.py`

**Interfaces:**
- Produces:
  - `class ApprovalApi(Construct)` props `config`, `audit: AuditStore`, `code: lambda_.Code`; attributes `function` (handler `approval.handler.handler`, memory 256, timeout 10 s, no reserved concurrency, JSON logging, own `LogGroup` 90 days DESTROY, env `AUDIT_TABLE`, `APPROVAL_AUTH`, `APPROVER_ROLE_ARN` (`""` when None)), `api: apigw.HttpApi` (`create_default_stage=False`, description `Lab janitor approval endpoint`), `stage: apigw.HttpStage` (`stage_name="$default"`, `auto_deploy=True`, `throttle=ThrottleSettings(rate_limit=10, burst_limit=20)`, `access_log_settings=` an object implementing `apigw.IAccessLogSettings` (declare `@jsii.implements(apigw.IAccessLogSettings) class AccessLogSettings` in `approval_api.py` with properties `destination` = `apigw.LogGroupLogDestination(access_log_group)` and `format` = `aws_cdk.aws_apigateway.AccessLogFormat.custom(ACCESS_LOG_FORMAT)`; a plain dict does not deserialize, verified against aws-cdk-lib 2.275.0) where `ACCESS_LOG_FORMAT = '{"requestId":"$context.requestId","requestTime":"$context.requestTime","routeKey":"$context.routeKey","status":"$context.status","ip":"$context.identity.sourceIp","error":"$context.error.message"}'` (no `$context.path`), access log group 90 days DESTROY), `url: str` (`api.url` without the trailing slash, exposed as `url_without_trailing_slash` via `Fn.select`/`Fn.split` or simply `api.api_endpoint`; use `api.api_endpoint`, which has no trailing slash). Routes with `HttpLambdaIntegration("Handler", function)`: when `approval_auth in (link, both)`: `GET /approvals/{id}` and `POST /approvals/{id}` with no authorizer; when in `(iam, both)`: `POST /iam/approvals/{id}` with `HttpIamAuthorizer()` from `aws_apigatewayv2_authorizers`. Grants: `audit.grant_approval_reads`, `audit.grant_approval_updates`; `iam.PolicyStatement(actions=["states:SendTaskSuccess", "states:SendTaskFailure"], resources=["*"], sid="TaskCallbacks")`.
  - `class Executor(Construct)` props `config`, `audit`, `code`; attribute `function` (handler `executor.handler.handler`, memory 512, timeout 10 minutes, `reserved_concurrent_executions=1 if config.reserved_concurrency else None`, env `lambda_env()` plus `PLANS_BUCKET`, `AUDIT_TABLE`, `MIN_AGE_DAYS`, `DELETIONS_ENABLED` = `"true" if not config.dry_run else "false"`, `EVENT_BUS_NAME="default"`). Policy statements (sids are exact):
    - `ExecutorDeleteLabEc2`: Allow `ec2:DeleteSnapshot`, `ec2:DeleteVolume`, `ec2:ReleaseAddress` on `arn:<partition>:ec2:*::snapshot/*`, `arn:<partition>:ec2:*:<account>:volume/*`, `arn:<partition>:ec2:*:<account>:elastic-ip/*` with `StringEquals aws:ResourceTag/<LAB_TAG_KEY> = <LAB_TAG_VALUE>`; plus `StringEquals aws:RequestedRegion = [regions]` when `config.allowed_regions` is non-empty.
    - `ExecutorDeleteLabNamed`: Allow `cloudwatch:DeleteDashboards` on `arn:<partition>:cloudwatch::<account>:dashboard/<LAB_NAME_PREFIX>*` and `internetmonitor:GetMonitor`, `internetmonitor:UpdateMonitor`, `internetmonitor:DeleteMonitor` on `arn:<partition>:internetmonitor:*:<account>:monitor/<LAB_NAME_PREFIX>*` (same optional region condition).
    - `ExecutorDenyKept`: Deny the six mutating actions above on `*` with `StringEquals aws:ResourceTag/<KEEP_TAG_KEY> = <KEEP_TAG_VALUE>`.
    - `ExecutorReverify`: Allow `ec2:DescribeSnapshots`, `ec2:DescribeVolumes`, `ec2:DescribeAddresses`, `ec2:DescribeImages` on `*`; `cloudwatch:ListTagsForResource` on the dashboard ARN pattern.
    - `events:PutEvents` on `arn:<partition>:events:<region>:<account>:event-bus/default`; `audit.grant_plan_reads`, `audit.grant_run_event_writes`, `audit.grant_approval_reads`. The executor does **not** call `allow_plan_writes`.
  - `tests/infra/helpers.py` adds `APPROVAL_HANDLER = "approval.handler.handler"`, `EXECUTOR_HANDLER = "executor.handler.handler"`, `executor_statements(template)`.
  - Guardrail rewrite in `test_iam_guardrails.py`: every Allow statement in the template whose action starts with `Delete`/`Release`/`Terminate` belongs to the executor role **or** is exactly `logs:DeleteLogDelivery` on the state machine role; each executor Allow with a mutating action has either a `LAB-*` resource ARN or the lab-tag condition, and never `Resource: "*"`; `ExecutorDenyKept` exists with the keep-tag condition; the executor action set is exact (listed in the test); the detector and notifier sets are unchanged; every DynamoDB grant in the template carries a `LeadingKeys` condition except `dynamodb:Query`; `states:SendTask*` appear only on the approval handler role with `Resource: "*"`.
  - `nag.py` gains reasons: `TASK_CALLBACK_REASON = "states:SendTaskSuccess and SendTaskFailure do not support resource-level permissions; the task token is the capability"`, `BEARER_LINK_REASON = "Approval links are bearer ids by design (spec 4.3, ADR-0008); GET is read-only and POST needs a typed confirmation; the IAM route is available via approvalAuth"`, `EXECUTOR_DESCRIBE_REASON = "Re-verification Describe* calls do not support resource-level permissions"`.

- [ ] **Step 1: Write the failing tests**

```python
# test_approval_api.py (fixtures: template (link), plus module fixtures iam_template and both_template built with context approvalAuth and approverRoleArn "arn:aws:iam::123456789012:role/Approver")
def test_link_mode_exposes_only_the_two_link_routes(template): ...          # AWS::ApiGatewayV2::Route RouteKeys == {"GET /approvals/{id}", "POST /approvals/{id}"}; AuthorizationType NONE
def test_iam_mode_exposes_only_the_iam_route_with_aws_iam(iam_template): ...
def test_both_mode_exposes_all_three(both_template): ...
def test_stage_is_default_with_throttle_and_access_logs_without_path(template): ...   # Format string lacks "$context.path"
def test_handler_env_and_grants(template): ...                               # GetItem/UpdateItem with LeadingKeys APPROVAL#*; SendTask* on "*"; nothing on S3
def test_handler_has_no_reserved_concurrency(template): ...

# test_executor.py
def test_executor_env_deletions_enabled_follows_dry_run(template, live_template): ...   # "false" in template, "true" in live_template
def test_executor_delete_statements_are_double_locked(template): ...        # sids, resources, conditions exactly as specified
def test_executor_region_condition_only_when_configured(): ...              # context allowedRegions "us-east-1,eu-west-1" -> aws:RequestedRegion list
def test_executor_deny_kept(template): ...
def test_executor_reads_plans_and_approvals_writes_run_events(template): ...
def test_executor_cannot_list_or_tag(template): ...                         # no Describe other than the four; no Tag*/CreateTags; no sns

# test_iam_guardrails.py: rewrite per the Interfaces block
```

- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement the two constructs, wire them in `stack.py`, acknowledge cdk-nag findings**

Expected new findings to acknowledge (confirm the exact ids from `make nag` output): `AwsSolutions-APIG4` and `Serverless-...` authorization findings on the link routes (`BEARER_LINK_REASON`); `AwsSolutions-IAM5[Resource::*]` on the approval handler role (`TASK_CALLBACK_REASON`) and on the executor role (`EXECUTOR_DESCRIBE_REASON`); `AwsSolutions-IAM5[Resource::<...>/plans/*]` on executor (existing `PLANS_PREFIX_REASON`); the shared Lambda findings via `acknowledge_janitor_function`. If `AwsSolutions-APIG1` (access logging) appears, the stage configuration is wrong: fix it, do not acknowledge.

- [ ] **Step 4: Run suite, lint, strict synth, cfn-lint**
- [ ] **Step 5: Commit**

```bash
git add lab_janitor tests/infra tests/snapshots
git commit -m "feat(cdk): approval HTTP API and executor with double-locked delete policy"
```

---

### Task 8: CDK workflow: full state machine, stack outputs, snapshot

**Files:**
- Modify: `lab_janitor/workflow.py`, `lab_janitor/stack.py`
- Test: `tests/infra/test_workflow.py`, `tests/infra/test_app.py`, `tests/snapshots/LabJanitorStack.template.json`

**Interfaces:**
- Consumes: `Notifier`, `ApprovalApi`, `Executor`, `AuditStore.table`, `Notifications`.
- Produces: `Workflow` props add `executor: Executor`. State names (exact; `test_state_names_are_exact` lists them): `Defaults`, `Normalize`, `Detect`, `HasCandidates`, `NotifyReportOnly`, `NewApprovalId`, `RequestApproval`, `RecordRejected`, `MirrorRejected`, `NotifyRejected`, `Rejected`, `RecordExpired`, `MirrorExpired`, `NotifyExpired`, `Expired`, `Apply`, `RecordApplied`, `NotifyResult`, `Applied`, `NotifyFailure`, `RunFailed`.
  - `HasCandidates` true branch → `NewApprovalId`: `sfn.Pass` with `parameters={"approvalId": sfn.JsonPath.uuid()}` and `result_path="$.approvalRequest"`.
  - `RequestApproval`: `LambdaInvoke(notifier, integration_pattern=WAIT_FOR_TASK_TOKEN, payload={"mode": "approval", "taskToken": JsonPath.task_token, "approvalId": "$.approvalRequest.approvalId", "detect": "$.detect", "config": "$.config"}, task_timeout=sfn.Timeout.duration(Duration.hours(72)), result_path="$.approval")`; catches in order: `errors=["States.Timeout"]` → `RecordExpired` (`result_path="$.error"`), `errors=["Rejected"]` → `RecordRejected` (`result_path="$.error"`), `errors=["States.ALL"]` → `NotifyFailure`; same retry on `Lambda.TooManyRequestsException` as the other invokes.
  - The five DynamoDB states are `sfn.CustomState` with raw ASL (the `DynamoUpdateItem`/`DynamoPutItem` task constructs auto-grant `dynamodb:UpdateItem`/`PutItem` on the whole table without a `LeadingKeys` condition, which spec 5.4 forbids; `CustomState` generates no IAM, and `add_catch` works on it). Grants are added explicitly after the state machine exists: `audit.grant_run_event_writes(state_machine.role)` and `audit.grant_approval_updates(state_machine.role)`.
    `RecordRejected` (and `RecordExpired` with `EXPIRED`):

    ```json
    {"Type": "Task", "Resource": "arn:<partition>:states:::dynamodb:updateItem",
     "Parameters": {"TableName": "<table name>",
       "Key": {"pk": {"S.$": "States.Format('APPROVAL#{}', $.approvalRequest.approvalId)"}, "sk": {"S": "META"}},
       "UpdateExpression": "SET #s = :s, decidedAt = :t",
       "ConditionExpression": "#s IN (:pending, :deciding)",
       "ExpressionAttributeNames": {"#s": "status"},
       "ExpressionAttributeValues": {":s": {"S": "REJECTED"}, ":t": {"S.$": "$$.State.EnteredTime"}, ":pending": {"S": "PENDING"}, ":deciding": {"S": "DECIDING"}}},
     "ResultPath": null}
    ```

    followed by `.add_catch(<mirror state>, errors=["DynamoDB.ConditionalCheckFailedException"], result_path="$.recordError")` and `.next(<mirror state>)`.
    `MirrorRejected` / `MirrorExpired` / `RecordApplied`: `putItem` with `Item` `{"pk": {"S.$": "States.Format('RUN#{}', $.detect.runId)"}, "sk": {"S.$": "States.Format('APPROVAL#{}', $.approvalRequest.approvalId)"}, "event": {"S": "REJECTED" | "EXPIRED" | "APPROVED"}, "at": {"S.$": "$$.State.EnteredTime"}, "actor": {"S": "statemachine"}, "detail": {"S.$": "States.JsonToString($.error)" | "States.JsonToString($.apply)"}}` (the detail is stored as a JSON string: the ASL DynamoDB integration has no map-from-path helper), `ConditionExpression` `attribute_not_exists(pk) AND attribute_not_exists(sk)`, `ResultPath` null, same conditional-failure catch to the next state. `RecordApplied` adds `"approvedBy": {"S.$": "States.JsonToString($.approval.approvedBy)"}`. The table name is `audit.table.table_name` (a token; `CustomState` accepts tokens in the dict) and the partition is `Stack.of(self).partition`.
  - `NotifyRejected` / `NotifyExpired` / `NotifyResult`: `LambdaInvoke(notifier)` with payloads `{"mode": "rejected" | "expired" | "result", "detect": "$.detect", "config": "$.config", "error": "$.error"}` (rejected, expired) and `{"mode": "result", "detect", "config", "approval": "$.approval", "apply": "$.apply"}` (result); `payload_response_only=True`, `result_path=JsonPath.DISCARD`, catch all → `NotifyFailure`; each followed by its `sfn.Succeed` (`Rejected`, `Expired`, `Applied`).
  - `Apply`: `LambdaInvoke(executor, payload={"detect": "$.detect", "config": "$.config", "approval": "$.approval", "approvalId": "$.approvalRequest.approvalId"}, payload_response_only=True, result_path="$.apply")`, throttle retry, catch all → `NotifyFailure`.
  - `NotifyFailure` message guard (Plan 1 follow-up): a `Pass` named `NotifyFailure`? No: keep `NotifyFailure` as the `SnsPublish`, and make the message `States.Format('Execution {} failed: {}', $$.Execution.Id, States.JsonToString($.error))` so a missing `Cause` cannot raise. Update the existing test accordingly.
  - Outputs move to `LabJanitorStack` (`CfnOutput(self, "StateMachineArn", ...)`, `PlansBucketName`, `AuditTableName`, plus `ApprovalApiUrl`), so the logical ids are exactly those names.
  - `stack.py` wiring order: notifications → audit → detector → notifier → approval api → executor → workflow → `notifications.restrict_publishers(notifier role arn)` → `notifier.function.add_environment("APPROVAL_URL", approval_api.url)` → outputs.

- [ ] **Step 1: Write the failing tests**

```python
def test_state_names_are_exact(template): ...                                  # the 21 names
def test_request_approval_waits_for_task_token_with_72h_timeout(template): ...  # Resource ends with ".waitForTaskToken"; TimeoutSeconds 259200; Parameters include "TaskToken.$": "$$.Task.Token", "approvalId.$": "$.approvalRequest.approvalId"; ResultPath "$.approval"
def test_request_approval_catches_timeout_then_rejected_then_all(template): ...
def test_new_approval_id_uses_states_uuid(template): ...                        # "approvalId.$": "States.UUID()"
def test_apply_reads_detect_values_not_approval(template): ...                  # Apply Parameters: "detect.$": "$.detect"; nothing reads "$.approval.planVersionId"
def test_record_states_are_conditional_and_catch_conditional_failures(template): ...
def test_mirror_items_are_conditional_puts_under_run_partition(template): ...
def test_notify_failure_message_survives_missing_cause(template): ...
def test_state_machine_role_cannot_invoke_the_approval_handler(template): ...   # lambda:InvokeFunction resources: detector, notifier, executor only
def test_state_machine_dynamodb_grants_carry_leading_keys(template): ...
def test_outputs_are_at_stack_level(template): ...                              # template.to_json()["Outputs"] keys == {"StateMachineArn", "PlansBucketName", "AuditTableName", "ApprovalApiUrl"}
def test_no_output_contains_an_email(template): ...
```

- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement the workflow and stack changes**

`JsonPath.DISCARD` renders `ResultPath: null` on CDK tasks; in the raw ASL dicts write `"ResultPath": None`. `test_state_machine_dynamodb_grants_carry_leading_keys` must find exactly two DynamoDB Allow statements on the state machine role: `PutItem` with `RUN#*` and `UpdateItem` with `APPROVAL#*`.

- [ ] **Step 4: Strict synth, snapshot, cfn-lint**

Run: `make lint && make nag && UPDATE_SNAPSHOTS=1 .venv/bin/pytest tests/infra/test_snapshot.py -q && git diff --stat tests/snapshots && make test && .venv/bin/cfn-lint --ignore-checks W3005 -- cdk.out/*.template.json`
Expected: exit 0 everywhere; review the snapshot diff for anything unexpected (an email, an account id, an unconditioned DynamoDB grant).

- [ ] **Step 5: Commit**

```bash
git add lab_janitor tests
git commit -m "feat(cdk): full approval workflow with record and mirror states; outputs at stack level"
```

---

### Task 9: `scripts/approve.py`, runbook and follow-ups

**Files:**
- Create: `scripts/approve.py`, `tests/unit/test_approve_script.py`
- Modify: `docs/runbook.md`, `docs/superpowers/plans/2026-11-03-plan-1-followups.md`, `README.md` (two lines under Configuration), `pyproject.toml` (`known-first-party` unchanged; add `scripts` to ruff paths only if excluded)

**Interfaces:**
- Produces: `scripts/approve.py` CLI: `python scripts/approve.py --url https://<api-id>.execute-api.<region>.amazonaws.com <approvalId> approve|reject [--profile P] [--region R]`. Builds the request with `botocore.auth.SigV4Auth` over `botocore.awsrequest.AWSRequest` (`service_name="execute-api"`, region from `--region` or the session), body `{"decision": "approve"}`, header `Content-Type: application/json`, sends with `urllib.request`, prints the status code and JSON body, exits 0 on 2xx and 1 otherwise. Function `signed_request(url: str, body: bytes, credentials, region: str) -> AWSRequest` is the testable unit (asserts the `Authorization` header starts with `AWS4-HMAC-SHA256` and contains `execute-api`).
- Runbook sections to add: "Approval flow" (what the email contains in each `approvalAuth` mode, the 72 h window, what `409`/`410` mean, how to find a record in the audit table by `APPROVAL#<id>` and the mirror under `RUN#<runId>`), "IAM approval" (bootstrap the CLI: `--profile <profile>`; the caller role must match `approverRoleArn`), "Dress rehearsal with deletions disabled" (the Task 10 procedure, generalized), update "Things you will see on first deploy" with the third subscription email and the API URL output, and update the outputs paragraph (now stack-level names).
- Follow-ups file: move the items this plan folded in to a "Done in Plan 2" list; keep the rest under Plan 3 with the note "deferred from Plan 2".

- [ ] **Step 1: Write the failing test** (`test_signed_request_has_sigv4_authorization_header`, `test_cli_rejects_other_decisions` via `argparse` error on `maybe`)
- [ ] **Step 2: Run to verify it fails**
- [ ] **Step 3: Write the script and the docs**
- [ ] **Step 4: Run suite and lint**
- [ ] **Step 5: Commit**

```bash
git add scripts tests/unit/test_approve_script.py docs README.md
git commit -m "docs: approval runbook and SigV4 approval CLI"
```

---

### Task 10: Deploy in dry-run and run a dress rehearsal with deletions disabled

**Files:**
- Modify: `docs/runbook.md` (only if the rehearsal reveals a wrong instruction), `docs/superpowers/plans/2026-11-03-plan-1-followups.md` (record what the rehearsal found)

**Interfaces:**
- Consumes: everything above; the deployed `LabJanitorStack` from Plan 1.
- Produces: a live run that went through approval and ended with every item SKIPPED `deletions-disabled`; no resource deleted.

- [ ] **Step 1: Owner deploys** (the agent prints the exact command and waits):

```bash
cd <repo> && export AWS_PROFILE=<profile> AWS_REGION=us-east-1 && npx cdk deploy --require-approval broadening -c reservedConcurrency=false -c reportEmail=<owner> -c alertEmail=<owner>
```

Expected: `UPDATE_COMPLETE`; a third subscription email (approvals) to confirm; output `ApprovalApiUrl`.

- [ ] **Step 2: Verify with boto3 (agent, read-only)**: three subscriptions confirmed (none `PendingConfirmation`); executor function env `DELETIONS_ENABLED=false`; state machine definition contains `RequestApproval` with `waitForTaskToken`; `GET <ApprovalApiUrl>/approvals/does-not-exist` returns `404` with the hardening headers.

- [ ] **Step 3: Owner creates a decoy** (agent prints the command): a CloudWatch dashboard `LAB-decoy-plan2` with an empty body in us-east-1 (`aws cloudwatch put-dashboard --dashboard-name LAB-decoy-plan2 --dashboard-body '{"widgets":[]}'`).

- [ ] **Step 4: Agent starts a live execution**: `{"dryRun": false, "minAgeDays": 7}` (the executor is still disarmed by `DELETIONS_ENABLED=false`). Expected: execution waits in `RequestApproval`; the approval email arrives with one candidate (`cw-dashboard LAB-decoy-plan2`); the weekly report arrives without the DRY RUN marker; the audit table has `APPROVAL#<id>/META` with `status PENDING`.

- [ ] **Step 5: Owner opens the link**, enters a wrong code first (expect `400` and the record back to `PENDING`), then the right code and approves. Expected: execution continues to `Apply`, ends `SUCCEEDED`; result email says `0 deleted, 0 failed, 1 skipped` with reason `deletions-disabled`; audit table has `RESOURCE#global#cw-dashboard#LAB-decoy-plan2#SKIPPED` and the mirror `RUN#<runId>/APPROVAL#<id>` with event `APPROVED`; EventBridge default bus received `janitor.resource.skipped` (check via the executor log line and, if possible, a CloudWatch Logs Insights query is not needed: the audit record suffices). A second submit of the same form returns `409`.

- [ ] **Step 6: Owner deletes the decoy** (`aws cloudwatch delete-dashboards --dashboard-names LAB-decoy-plan2`) and the agent confirms it is gone.

- [ ] **Step 7: Record outcomes**: a short "Dress rehearsal 2026-11-xx" paragraph in the runbook's "First dry-run" neighbor section with the run id and approval id (no email, no account id), and any defect found goes to the follow-ups file. Commit:

```bash
git add docs
git commit -m "docs: dress rehearsal of the approval flow with deletions disabled"
```

---

## Self-review notes

- Spec coverage: 4.1 approvals topic (Task 6); 4.2 every state (Task 8), compact Detect result (unchanged), manual runs (Task 10); 4.3 GET/POST sequence, status codes, headers, id generation (Tasks 4 and 8; id is a `States.UUID()` instead of 32 random bytes, recorded in the Architecture paragraph and the spec v3 note to add in Plan 4), logging hash (Task 4), `SendTask*` on `*` (Task 7), IAM route and `scripts/approve.py` (Tasks 4, 7, 9), stage throttle (Task 7), reserved concurrency only on detector and executor (Task 7); 4.5 approval family, mirrors, conditional puts, ELIGIBLE (Tasks 1, 2, 8); 4.6 events (Task 5); 4.7 roles (Tasks 6, 7, 8); 4.8 EIP rule (Task 2); 4.9 executor (Task 5); 5.3 keys (Task 6); 5.4 named gates (Tasks 4, 5, 7, 8).
- Deviations ruled here: `RecordApplied` is a mirror put with counts and approver, not a second `RUN#SUMMARY` (the detector owns that key); rejected/expired recording is two states each (update plus mirror) because each DynamoDB integration is one call; HTTP APIs have no resource policy, so the IAM route's principal check lives in the handler (spec v3).
- Type consistency: `ApprovalRecord` fields, `ApprovalStore` method names, `was_eligible_before` callback name, `summarize_plan`/`render_summary`, `executor` event shape `{detect, config, approval, approvalId}`, `apply` result shape `{deleted, failed, skipped, items}` are used identically in Tasks 3, 4, 5 and 8.
- Proportion: this plan is shorter than the executor and handler code it describes; bodies appear only where an ordering or an exact message is pinned.
