# Plan 4: Story and Publication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the working, deployed janitor into something a stranger can read: a README that says what it does and proves it, the decisions behind it, a threat model, evidence of real operation, and a first push to GitHub with nothing personal in the tree.

**Architecture:** No runtime code changes. The work is documents, and the documents are held to the code by tests: a `tests/docs` package checks that links resolve, that the README's tables and diagram name what the code names, that every ADR cited in code exists, and that no personal token is in the tree. Facts come from the repo and from read-only calls to the owner's account; nothing is written from memory.

**Tech Stack:** Markdown and Mermaid (rendered by GitHub), pytest for the document tests, the CloudWatch `GetMetricWidgetImage` API for two graphs, `gh` for the repository.

**Spec:** `docs/superpowers/specs/2026-11-02-lab-janitor-portfolio-design.md`, sections 2 (goals), 5.1 (repository), 5.5 (documentation) and 7 (phase 6, Story).

## Owner decisions (taken on 2026-11-12)

1. Commit author identity stays as it is. No author rewrite.
2. The names of the owner's real resources that appear in the evidence (two S3 buckets, one CloudFront distribution id, one dashboard) are replaced with example names, in the file and in the history. The decoys keep their ids: they no longer exist.
3. Everything under `docs/superpowers/` is published (spec, plans, follow-ups), with the local checkout path and the SSO profile name replaced in the tree.
4. Publication goes to a private repository first (`MetMatDav/aws-lab-janitor`), created and pushed by the agent with `gh`. The owner makes it public after looking at it.

## Decisions that depart from the spec

1. The spec's ten ADRs are written, plus four for decisions taken while building: the reminder as an email from a second state machine (0011), refusing to publish without a subscriber instead of an alarm on delivery metrics (0012), the one-hour apply window and strict flags (0013), and the three kinds of tests (0014).
2. The spec is not rewritten to match the code. It keeps its text as the design that was approved and gains a last section, "As built", listing each difference with the reason. A reader sees both the plan and what happened to it.
3. "Redacted report email" and "CloudWatch graph of weekly invocations since August 2026" (spec 5.5): the graph is generated from the metrics of the old function, which outlive it; the report is the stored `report.txt` of the controlled run with real names replaced. Screenshots of the emails are optional and the owner's to add.
4. A `docs/testing.md` that the spec does not list: how the suite is layered and what was only ever exercised live.

## Global Constraints

- Everything in the repo in English. No em dashes anywhere.
- No account id, email address, IP address or secret in any tracked file. Allowed placeholders: `123456789012`, `111122223333`, `example.com`, `<account-id>`. The commit author identity is the one exception, by owner decision 1.
- Every factual sentence in a new document is checked against the code, a test, an evidence file or a read-only call before it is written. A claim that cannot be checked is cut or marked as not verified.
- Nothing is described as tested, verified or exercised live unless the runbook or `docs/evidence/` records it. The untested list is stated wherever a reader would otherwise assume coverage: the IAM approval route, the expiry path (until Task 7), the Elastic IP and Internet Monitor deleters.
- No exact test counts in the README; they drift. `docs/testing.md` may give counts with a date.
- No runtime code changes. If a document shows the code to be wrong, stop and record it in the follow-ups file; fix it in its own commit with its own failing test.
- Python 3.13, `pytest -q`, `ruff check . && ruff format --check .` and `npx cdk synth --strict` stay green after every task.
- Deploys, `cdk destroy` and making the repository public are the owner's. The agent's AWS calls are read-only, with the profile the owner uses.

## Review Focus

1. **A README table that drifts from the code.** A reader trusts "can delete" and "never touches". Test `test_readme_tables_match_the_code` (Task 6).
2. **A dead link or anchor.** Fifteen documents link to each other and to files. Test `test_relative_links_and_anchors_resolve` (Task 1).
3. **A personal token left in the tree or the history.** Test `test_no_forbidden_token_in_tracked_files` and the history scan (Task 1, run again in Task 8).
4. **A diagram that names states the machine does not have.** Test `test_readme_diagram_names_only_real_states` (Task 6).
5. **A quickstart that only works on the author's machine.** Fresh-clone check without AWS credentials (Task 8).

## File Structure

```
README.md                              rewritten (Task 6)
docs/adr/README.md                     index (Task 3)
docs/adr/0001-...md to 0014-...md      one decision each (Task 3)
docs/threat-model.md                   (Task 4)
docs/testing.md                        (Task 5)
docs/evidence/legacy-weekly-runs.md    the old janitor's runs (Task 5)
docs/evidence/legacy-weekly-runs.png   graph (Task 5)
docs/evidence/report-2026-11-12.txt    stored report, names replaced (Task 5)
docs/evidence/controlled-deletion-2026-11-12-audit.json   names replaced (Task 1)
docs/superpowers/specs/...design.md    status line and section 12 (Task 2)
lab_janitor/nag.py                     reasons reworded (Task 2)
tests/docs/__init__.py
tests/docs/hygiene.py                  forbidden tokens, shared by test and history scan (Task 1)
tests/docs/markdown.py                 link, anchor, table and mermaid parsing (Task 1)
tests/docs/test_hygiene.py             (Task 1)
tests/docs/test_links.py               (Task 1)
tests/docs/test_adrs.py                (Task 3)
tests/docs/test_readme.py              (Task 6)
```

---

### Task 1: Document tests and a clean tree

**Files:**
- Create: `tests/docs/__init__.py`, `tests/docs/hygiene.py`, `tests/docs/markdown.py`, `tests/docs/test_hygiene.py`, `tests/docs/test_links.py`
- Modify: `docs/evidence/controlled-deletion-2026-11-12-audit.json`, the three plan files that name the local checkout path and the SSO profile

**Interfaces:**
- Produces: `tests.docs.hygiene.find_forbidden(text: str) -> list[str]` (the offending tokens, hashed ones reported as `sha256:<first 12>`); `tests.docs.hygiene.tracked_text_files() -> list[Path]` (`git ls-files`, text files only); `python -m tests.docs.hygiene --history` (scans `git log -p --all`, exit 1 if anything is found).
- Produces: `tests.docs.markdown.links(path) -> list[tuple[str, str | None]]` (relative target, anchor), `markdown.anchors(path) -> set[str]` (GitHub heading slugs: lowercase, punctuation other than hyphen and space dropped, spaces to hyphens, backticks dropped), `markdown.tables(text, heading) -> list[list[str]]`, `markdown.mermaid_blocks(text) -> list[str]`.

- [ ] **Step 1: Write the failing tests.**

`test_hygiene.py`:

```python
def test_no_forbidden_token_in_tracked_files():
    found = {
        str(path): hits
        for path in hygiene.tracked_text_files()
        if (hits := hygiene.find_forbidden(path.read_text(errors="replace")))
    }
    assert found == {}


@pytest.mark.parametrize(
    "text",
    [
        "account " + "9999" + "88887777" + " here",   # a 12-digit id that is not a placeholder
        "write to someone@" + "mailbox.test",         # an address outside the allowed domains
        "from " + ".".join(["8", "8", "8", "8"]),     # a routable IPv4 address
        "cd /home/" + "someone/project",              # a home directory
    ],
)
def test_find_forbidden_catches(text):
    assert hygiene.find_forbidden(text)


@pytest.mark.parametrize(
    "text",
    [
        "arn:aws:iam::123456789012:role/x and 111122223333",
        "ci@example.com, noreply@anthropic.com, no-reply@sns.amazonaws.com",
        "198.51.100.7, 203.0.113.9, 192.0.2.1, 127.0.0.1, 0.0.0.0",
        "aws-cdk-lib 2.275.0 and botocore 1.43.123",
        "a plan sha256 " + "3" * 64,
    ],
)
def test_find_forbidden_allows(text):
    assert hygiene.find_forbidden(text) == []
```

The examples are assembled at run time so that this file, and this plan, pass the test they describe. The owner's real resource names and the account id are matched by hash, so the test does not publish what it forbids: `hygiene.FORBIDDEN_SHA256` is a frozenset of `sha256(token.lower())` hex digests, and `find_forbidden` hashes every run of `[A-Za-z0-9._-]+` in the text. Compute the digests from the four names in today's audit file and from the account id (`sts get-caller-identity`), and add the SSO profile name the same way.

`test_links.py`:

```python
DOCUMENTS = [Path("README.md"), *sorted(Path("docs").rglob("*.md"))]


@pytest.mark.parametrize("document", DOCUMENTS, ids=str)
def test_relative_links_and_anchors_resolve(document):
    for target, anchor in markdown.links(document):
        destination = (document.parent / target).resolve() if target else document.resolve()
        assert destination.exists(), f"{document}: {target}"
        if anchor and destination.suffix == ".md":
            assert anchor in markdown.anchors(destination), f"{document}: {target}#{anchor}"
```

Links with a scheme (`https:`, `mailto:`) are skipped; links inside fenced code are not links.

- [ ] **Step 2: Run them.** `pytest -q tests/docs`. Expected: the hygiene test fails on the audit file (four hashed names) and on the three plan files (home directory, profile name); the link test fails wherever a link is already dead, or passes.

- [ ] **Step 3: Implement `hygiene.py` and `markdown.py`**, then fix what the tests found: in the audit file replace each real name with `example-bucket-1`, `example-bucket-2`, `EXAMPLEDISTRIBUTION`, `example-dashboard` (in `resourceId`, `sk` and any `detail`), and say so in the file's `note`; in the plan files replace the checkout path with `<repo>` and the profile name with `<profile>`. Fix any dead link.

- [ ] **Step 4: Rewrite the history of the audit file.** The file entered the history in the commit "docs: controlled deletion test and cutover record". Rewrite that commit and its descendants so the file never held the real names (`git filter-repo --replace-text` if installed, otherwise `git filter-branch --tree-filter` over that range; no author change). Then:

Run: `python -m tests.docs.hygiene --history`
Expected: exit 0, nothing found.

- [ ] **Step 5: Run everything.** `pytest -q`, ruff. Expected: green.

- [ ] **Step 6: Commit.** `test: documents are checked for dead links and personal tokens`

### Task 2: The spec says what was built

**Files:**
- Modify: `docs/superpowers/specs/2026-11-02-lab-janitor-portfolio-design.md` (status line; new section 12), `lab_janitor/nag.py`, `lab_janitor/audit.py` (reason strings only), `docs/superpowers/plans/2026-11-02-plan-1-bootstrap-and-detector.md` (the cfn-lint command without `--`)

- [ ] **Step 1: Section 12, "As built (2026-11-12)".** One table: what the spec says, what was built, why, where to look. One row per difference; read the three plans' "Decisions that depart from the spec", the follow-ups file and `docs/known-limitations.md` to collect them. At least these: the approval id is a `States.UUID()` from the state machine, not 32 random bytes; the link `POST` verifies the code first and claims second; the execution timeout is 74 h; the notifier holds `dynamodb:UpdateItem` on `APPROVAL#*`; the reminder is an email from a second state machine, not a metric and an alarm; the IAM simulation is a Python module; there is no alarm on delivery, the notifier checks subscriptions; an approval is applied only within an hour of the decision; flags accept only `true` or `false`; `minAgeDays` is capped at 3650; the audit sort key carries the kind; the Elastic IP rule (two live runs); `createTrail` deployed as `dataEventsOnly`; the alarms are four. Status line: `v3: built and deployed; section 12 lists what changed while building`.

- [ ] **Step 2: Reword the cdk-nag reasons that are only partly true** (follow-ups file, "Plan 4"): the shared dead-letter queue also receives SNS redrives; the Python 3.13 reason must not promise a roadmap entry that does not exist; the `AwsSolutions-S1` reason depends on `createTrail`. Each reason must still be true in the three trail modes.

Run: `pytest -q tests/infra && npx cdk synth --strict --quiet -c reportEmail=ci@example.com` for `createTrail` `full`, `dataEventsOnly` and `none`
Expected: green, no unacknowledged finding.

- [ ] **Step 3: Commit.** `docs: the spec records what changed while building`

### Task 3: Architecture decision records

**Files:**
- Create: `docs/adr/README.md`, `docs/adr/0001-two-retry-layers-and-a-shared-dead-letter-queue.md`, `0002-one-boto3-session-and-512-mb.md`, `0003-global-and-regional-resources.md`, `0004-the-double-lock-code-and-iam.md`, `0005-dead-letter-alarm-treats-missing-data-as-healthy.md`, `0006-never-delete-compute-or-data.md`, `0007-plan-approval-apply.md`, `0008-approval-by-email-link.md`, `0009-three-topics-and-an-alarm-backstop.md`, `0010-audit-table-write-rules-and-cloudtrail-scope.md`, `0011-the-reminder-is-an-email-from-a-second-state-machine.md`, `0012-refuse-to-publish-instead-of-alarming-on-delivery.md`, `0013-an-approval-is-good-for-one-hour.md`, `0014-three-kinds-of-tests.md`
- Test: `tests/docs/test_adrs.py`

- [ ] **Step 1: Write the failing test.**

```python
ADRS = sorted(Path("docs/adr").glob("[0-9][0-9][0-9][0-9]-*.md"))
SECTIONS = ["Status", "Context", "Decision", "Consequences", "Where to look"]


def test_there_are_fourteen_numbered_without_gaps():
    assert [path.name[:4] for path in ADRS] == [f"{n:04d}" for n in range(1, 15)]


@pytest.mark.parametrize("adr", ADRS, ids=lambda path: path.name[:4])
def test_adr_has_the_five_sections_in_order(adr):
    headings = [line[3:].strip() for line in adr.read_text().splitlines() if line.startswith("## ")]
    assert headings == SECTIONS


def test_index_lists_every_adr_once():
    index = Path("docs/adr/README.md").read_text()
    for adr in ADRS:
        assert index.count(f"({adr.name})") == 1


def test_every_adr_cited_in_code_exists():
    cited = set()
    for path in [*Path("lab_janitor").rglob("*.py"), *Path("src").rglob("*.py")]:
        cited |= set(re.findall(r"ADR-(\d{4})", path.read_text()))
    assert cited <= {path.name[:4] for path in ADRS}
```

Run: `pytest -q tests/docs/test_adrs.py`. Expected: FAIL, no files.

- [ ] **Step 2: Write the fourteen ADRs.** Each at most a page. `Status` is `Accepted` with the date the decision was taken (from the commit or the plan that took it). `Context` is the problem and the forces, in the past tense. `Decision` is one paragraph in the present tense. `Consequences` lists what it costs as well as what it buys, and names the alternative that was rejected and why. `Where to look` is a list of paths: the code that implements it and the test that would fail if it were undone. Before writing each one, open the code it describes; ADRs 0001, 0002 and 0005 come from the old single-Lambda janitor and are written about the code as it is now (if the decision no longer holds, the ADR says `Superseded` and by what). ADR 0012 tells the false alarm of 2026-11-12 with the evidence file; ADR 0008 links `docs/known-limitations.md`; ADR 0014 names the three suites and what each found.

- [ ] **Step 3: Run.** `pytest -q tests/docs`. Expected: PASS, including the link test over the new files.

- [ ] **Step 4: Commit.** `docs: architecture decision records 0001 to 0014`

### Task 4: Threat model

**Files:**
- Create: `docs/threat-model.md`

- [ ] **Step 1: Write it**, in this order: what is protected (the owner's resources from wrongful deletion; the approval decision; the audit record); who is considered (someone who sees an approval URL; someone who can create or tag resources; someone who can start executions or write the plans bucket; a read-only insider; the operator making a mistake) and who is not (an account administrator, AWS itself); a trust-boundary diagram in Mermaid; one table with the worst each role can do, a row per role (detector, notifier, approval handler, executor, the two state machines, the scheduler), filled from the synthesized policies and `docs/evidence/iam-simulation.txt`, not from intent; the approval channel (possession of the mailbox is the authority, forwarding is delegation, the id in the URL can leak but cannot be guessed, a mail scanner cannot approve because `GET` changes nothing and `POST` needs a typed code, the IAM route as the stronger option); the audit trail (tamper-evident, not tamper-proof: what the conditional writes, the bucket policy, versioning and CloudTrail each add, and what an administrator can still do); the Elastic IP in a default VPC note from the spec; what the adversarial pass tried and what held (from the follow-ups file); and residual risks, each linking to `docs/known-limitations.md` or the follow-ups file.

- [ ] **Step 2: Check every row of the role table** against the synthesized template: for each role, list its allowed actions with `tests/infra/helpers.role_statements` in a scratch script and compare.

- [ ] **Step 3: Run** `pytest -q tests/docs`. **Commit.** `docs: threat model`

### Task 5: Testing notes and evidence

**Files:**
- Create: `docs/testing.md`, `docs/evidence/legacy-weekly-runs.md`, `docs/evidence/legacy-weekly-runs.png`, `docs/evidence/report-2026-11-12.txt`

- [ ] **Step 1: The old janitor's runs.** From the retained log group of the old stack (name in the follow-ups file), list every invocation from its `REPORT` lines: date, weekday, duration, memory. Expected: 16 scheduled Monday runs from 2026-07-27 to 2026-11-09, between 21 and 27 seconds, plus the manual runs of 2026-07-26. The markdown file holds the table and says what the old janitor was (one Lambda, delete then email, deleted only one self-test dashboard, as its logs turned out to show). The graph: `cloudwatch get_metric_widget_image` for `AWS/Lambda` `Invocations` and `Duration` of the old function, weekly, 2026-07-20 to 2026-11-12, saved as PNG. Check the image shows no account id.

- [ ] **Step 2: The stored report of the controlled run.** `plans/20261112T195659Z-bbb8/report.txt` from the plans bucket, with the same four name replacements as Task 1. Run the hygiene test.

- [ ] **Step 3: `docs/testing.md`.** The layers and what each is for: unit tests with botocore `Stubber`; infrastructure tests (assertions, snapshot, cdk-nag strict); the IAM policy simulation against the live policy engine; the adversarial suite and its three footholds; the property tests and the two hypothesis profiles; how to run each (`make test`, `make simulate`, `HYPOTHESIS_PROFILE=ci`). Then what was exercised live, as a table with the date and the evidence file for each: dry-run, approval, rejection, wrong code, reminder, failure alarm, data events, real deletion. Then what never ran live. Counts by suite with today's date.

- [ ] **Step 4: Tell the owner** the log group can now be deleted, with the command, and that screenshots of the approval and result emails can be added to `docs/evidence/` with personal data covered.

- [ ] **Step 5: Run** `pytest -q tests/docs`. **Commit.** `docs: how it is tested, and evidence of the old and new janitor running`

### Task 6: README

**Files:**
- Modify: `README.md` (rewritten)
- Test: `tests/docs/test_readme.py`

**Interfaces:**
- Consumes: `markdown.tables`, `markdown.mermaid_blocks` (Task 1); `shared.findings.KINDS`; `executor.handler.DELETABLE_KINDS`; `tests.infra.helpers.state_machine_definition`.

- [ ] **Step 1: Write the failing tests.**

```python
README = Path("README.md").read_text()


def first_column(heading: str) -> set[str]:
    return {row[0].strip("` ") for row in markdown.tables(README, heading)}


def test_readme_tables_match_the_code():
    detects = first_column("What it detects")
    deletes = first_column("What it can delete, after approval")
    assert detects == set(KINDS)
    assert deletes == set(DELETABLE_KINDS)
    assert deletes <= detects


def test_never_touches_lists_no_deletable_kind():
    assert first_column("What it never touches").isdisjoint(DELETABLE_KINDS)


def test_readme_diagram_names_only_real_states(template):
    states = set(state_machine_definition(template)["States"])
    (diagram,) = [block for block in markdown.mermaid_blocks(README) if block.startswith("stateDiagram")]
    named = set(re.findall(r"\b([A-Z][A-Za-z]+)\b", diagram)) - {"Standard"}
    assert named and named <= states


def test_readme_has_the_sections_a_reader_needs():
    headings = [line[3:].strip() for line in README.splitlines() if line.startswith("## ")]
    assert headings == [
        "What it does",
        "How a deletion happens",
        "What it detects",
        "What it can delete, after approval",
        "What it never touches",
        "Try it in dry-run",
        "What it costs",
        "Evidence",
        "How it is tested",
        "Limits",
        "Why not aws-nuke, cloud-nuke or Cloud Custodian",
        "How it was built",
        "License",
    ]
```

The `template` fixture is the one in `tests/infra/conftest.py`; import it or move it to `tests/conftest.py`.

Run: `pytest -q tests/docs/test_readme.py`. Expected: FAIL.

- [ ] **Step 2: Write the README.** A reader with three minutes must come away knowing what it is, what it deletes, what it never touches and that it runs for real (spec section 2). Content per section:
  - Opening: the name, one paragraph, the CI badge, the license badge.
  - *What it does*: the weekly cycle in five sentences and one Mermaid flowchart of the components (schedule, state machine, the four functions, the plans bucket, the audit table, the three topics, the HTTP API, CloudTrail).
  - *How a deletion happens*: the `stateDiagram-v2` of the weekly machine using its real state names, then the double lock in a short paragraph (code re-verifies each item; IAM denies anything not lab-marked and anything keep-tagged), then the three things that must all be true for a deletion.
  - *What it detects* (every kind, how it is marked lab, whether it has a cost estimate), *What it can delete, after approval* (the five kinds, the rule for each, including that dashboards and monitors are proposed on first detection), *What it never touches* (compute and data stores, with the reason: no role holds those actions).
  - *Try it in dry-run*: prerequisites, bootstrap, the deploy command with context values and placeholder email, confirming the three subscriptions, starting an execution by hand, reading the report; then the one switch that arms deletions. Link the runbook for everything else.
  - *What it costs*: the janitor's own monthly cost by service, from the pricing of each service at this usage, next to the monthly cost of one forgotten NAT gateway from `shared/pricing.py`. State the date of the prices.
  - *Evidence*: sixteen weekly runs of the old janitor; the rehearsals; the controlled deletion; the false alarm that led to ADR 0012. Each a link.
  - *How it is tested*: one paragraph and a link to `docs/testing.md`; the two review passes of 2026-11-12 and what they found.
  - *Limits*: what never ran live; the three known limitations; the deliberate choices; what is not detected. Links.
  - *Why not aws-nuke, cloud-nuke or Cloud Custodian*: what each is for, in one sentence each, checked against each project's own README today, and what this project does differently (a human approves a specific plan; a lab account that is still in use).
  - *How it was built*: the spec, the four plans, the ADRs, the follow-ups; that it was built with Claude as a coding agent under the owner's direction and review.
  - *License*: MIT.

- [ ] **Step 3: Check every number and every command in it.** Each count, price, date and duration traced to a file; each command run, or dry-run, once.

- [ ] **Step 4: Run** `pytest -q`, ruff. **Commit.** `docs: README`

### Task 7: Two things that only time can show

**Files:**
- Modify: `docs/runbook.md`, `docs/superpowers/plans/2026-11-03-plan-1-followups.md`; Create: `docs/evidence/expiry-2026-11-14.md`

- [ ] **Step 1: After 2026-11-14 20:09 UTC**, read the execution of run `20261111T203848Z-138c`: path `RecordExpired`, `MirrorExpired`, `NotifyExpired`, `Expired`; record `EXPIRED`; the expiry email delivered; the reminder execution ended with `{"reminded": false}` or sent its email first. Write what was seen. If it did not happen this way, that is a defect: failing test first.
- [ ] **Step 2: After Monday 2026-11-16 14:00 UTC**, read the first scheduled run with deletions armed: it started by itself, with input `{"dryRun":false,"minAgeDays":7}`; what it proposed; the heartbeat alarm `OK`. One line in the runbook.
- [ ] **Step 3: Update** `docs/testing.md` (the expiry path moves to "exercised live"), the README's limits, and the follow-ups file. **Commit.** `docs: the expiry path and the first scheduled live run`

This task does not block Task 8; whichever date comes later, its commit is pushed afterwards.

### Task 8: Publication

- [ ] **Step 1: Everything green on a fresh clone, without AWS credentials.** `git clone` the repository into a temporary directory; with `AWS_PROFILE`, `AWS_ACCESS_KEY_ID` and `AWS_CONFIG_FILE` unset or pointing nowhere: `make install`, `make lint`, `make test`, `make nag`. Expected: all pass, as CI will run them.
- [ ] **Step 2: History scan.** `python -m tests.docs.hygiene --history`. Expected: nothing.
- [ ] **Step 3: Create the private repository and push.** `gh repo create MetMatDav/aws-lab-janitor --private --source . --remote origin --description "<the description of spec 5.1>"`, then `git push -u origin main`. Add the topics of spec 5.1 with `gh repo edit --add-topic`.
- [ ] **Step 4: Look at it on GitHub.** CI run green; the README renders, both Mermaid diagrams included; the badges resolve; five links clicked at random work; the file list shows no `cdk.out`, `.venv`, `.superpowers` or `cdk.context.json`.
- [ ] **Step 5: Hand over to the owner.** What to look at before making it public, and the command: `gh repo edit MetMatDav/aws-lab-janitor --visibility public --accept-visibility-change-consequences`. After it is public: tag `v1.0.0`.
- [ ] **Step 6: Close out.** Follow-ups file: what Plan 4 closed, what stays open (the IAM route live, the two deleters, the cosmetic list, the redeploy with reserved concurrency if it has not happened). Commit and push.

---

## Left open (not in this plan)

- Exercising the IAM approval route live (needs `approvalAuth=both` and an approver role in a deploy).
- Decoys for an Elastic IP and an Internet Monitor monitor.
- Aligning the infrastructure test fixtures with the `cdk.json` feature flags, so the snapshot equals a real synth.
- A CI matrix over the three `createTrail` modes.
- The three known limitations (`docs/known-limitations.md`).

## Self-review notes

- Spec coverage: 5.1 repository name, description, topics (Task 8); 5.5 README (Task 6), ADRs 0001 to 0010 (Task 3, plus four), runbook (exists since Plans 1 to 3), threat model (Task 4), evidence (Tasks 5 and 7; Plan 3 for the alarm history and the deletion run); section 2's three-minute and twenty-minute readers (Tasks 6 and 3 to 5); the anti-goal on personal data in the repo and its history (Tasks 1 and 8, with owner decision 1 as the stated exception); section 7 phase 6 (all tasks).
- Type consistency: `find_forbidden` and `tracked_text_files` are used with the same names in Tasks 1 and 8; `markdown.tables(text, heading)` and `markdown.mermaid_blocks(text)` in Tasks 1 and 6; the five ADR section names are the same in the test and in Step 2 of Task 3.
- Review Focus: lines 1 and 4 are tested in Task 6, line 2 and 3 in Task 1, line 5 is a verification step in Task 8.
- Proportion: test code appears where an assertion pins a document to the code; document bodies are described by their content, not written out.
