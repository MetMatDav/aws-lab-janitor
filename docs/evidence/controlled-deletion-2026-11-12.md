# Controlled deletion test: 2026-11-12

The first time the janitor deleted anything. Three decoys were created for the
purpose, proposed by a live run, approved by the owner through the emailed
link and deleted by the executor. A fourth decoy carried the keep tag and
survived. All times are UTC. The audit table items of the run are in
[controlled-deletion-2026-11-12-audit.json](controlled-deletion-2026-11-12-audit.json).

## Before the test

- The old stack (`AwsJanitorStack`) was redeployed in report-only mode at 18:32.
- The new stack was redeployed with `-c dryRun=false` at 19:51: executor
  `DELETIONS_ENABLED=true`, weekly schedule input `{"dryRun":false,"minAgeDays":7}`.
- Preview run `20261112T195333Z-8d4d` with `{"dryRun": true, "minAgeDays": 0}`:
  25 regions swept, zero candidates, four inventory items, none of them
  lab-marked. So nothing that already existed in the account could be proposed
  by the test.

## Decoys (us-east-1)

| Decoy | Id | Marked by | Expected |
| ----- | -- | --------- | -------- |
| CloudWatch dashboard | `LAB-decoy` | name prefix `LAB-` | deleted |
| EBS volume, 1 GiB gp3 | `vol-0001e0237f1d10359` | tag `Purpose=lab` | deleted |
| EBS snapshot A | `snap-0c95095ead1c139ec` | tag `Purpose=lab` | deleted |
| EBS snapshot B | `snap-069661b86a0bdf3b6` | tags `Purpose=lab` and `janitor=keep` | kept |

## The run

Run `20261112T195659Z-bbb8`, started by hand with
`{"dryRun": false, "minAgeDays": 0}`. Approval
`2e8fa9c9-fdf6-4d1b-9ff4-d4bcf523f244`. Plan sha256
`363682034293411e3002780839e5a37877472d3d6b2fcdc2ba93b2be6e7b8aa3`.

The plan held exactly three candidates:

```
[cw-dashboard] LAB-decoy (global) name prefix LAB-
[ebs-snapshot] snap-0c95095ead1c139ec (us-east-1) tag Purpose=lab, 0 days old, not referenced by any AMI
[ebs-volume] vol-0001e0237f1d10359 (us-east-1) tag Purpose=lab, available, 0 days old
```

Snapshot B was in the plan's inventory, not among the candidates, with the
reason `tagged janitor=keep`.

| Time | What |
| ---- | ---- |
| 19:56:59 | Run started; the detector stored the plan and wrote `DETECTED` and `PROPOSED` items |
| 19:57 | Weekly report and approval request delivered (SNS: 1 published, 1 delivered on each topic) |
| 19:59:11 | Approval page opened (`GET 200`) |
| 20:00:08 | Approved through the link with the confirmation code (`POST 200`); record `PENDING` to `APPROVED` |
| 20:00:09 | Executor started, armed; read the approval record and the approved plan version |
| 20:00:10 | `DELETED` dashboard, `DELETED` snapshot A |
| 20:00:11 | `DELETED` volume; execution `SUCCEEDED`; result email delivered |

State path: `Defaults`, `Normalize`, `Detect`, `HasCandidates`,
`NewApprovalId`, `StartReminder`, `RequestApproval`, `Apply`,
`RecordApplied`, `NotifyResult`, `Applied`.

Result: `3 deleted, 0 failed, 0 skipped`.

## What was checked afterwards

- The dashboard, the volume and snapshot A no longer exist
  (`ResourceNotFound`, `InvalidVolume.NotFound`, `InvalidSnapshot.NotFound`).
- Snapshot B still existed, `completed`, with both tags. It was deleted by hand
  after the check.
- The audit table holds three `DELETED` items written by `lambda:executor`,
  each with the approval id and the plan sha256, and the `APPROVED` mirror
  written by the state machine with the counts.
- The decided approval answers `409` to a new request.
- The four alarms stayed `OK`.

## CloudTrail

Management events, from Event history, filtered by the executor's session name
(`LabJanitorStack-ExecutorFunction90B119DB-DVSGVLYA0crf`):

| Event | Time | Region | Request |
| ----- | ---- | ------ | ------- |
| `DeleteDashboards` | 20:00:10 | us-east-1 | `dashboardNames: [LAB-decoy]` |
| `DeleteSnapshot` | 20:00:10 | us-east-1 | `snapshotId: snap-0c95095ead1c139ec` |
| `DeleteVolume` | 20:00:11 | us-east-1 | `volumeId: vol-0001e0237f1d10359` |

Data events, from the stack's own trail, for the same run:

| Time | Event | Role session | Key |
| ---- | ----- | ------------ | --- |
| 19:57:17 | `PutObject` | detector | `plans/20261112T195659Z-bbb8/plan.json` |
| 19:57:18 | `PutItem` (12) | detector | `DETECTED`, `PROPOSED` and `RUN#SUMMARY` items |
| 19:57:18 | `GetObject`, `PutItem` | notifier | the plan, then the approval record |
| 19:59:13 | `GetItem` | approval handler | the approval record |
| 20:00:09 | `GetItem`, `GetObject` | executor | the approval record, then the plan |
| 20:00:10 | `PutItem` | executor | `RESOURCE#global#cw-dashboard#LAB-decoy#DELETED` |
| 20:00:10 | `PutItem` | executor | `RESOURCE#us-east-1#ebs-snapshot#snap-0c95095ead1c139ec#DELETED` |
| 20:00:11 | `PutItem` | executor | `RESOURCE#us-east-1#ebs-volume#vol-0001e0237f1d10359#DELETED` |
| 20:00:11 | `PutItem` | state machine | `APPROVAL#2e8fa9c9-fdf6-4d1b-9ff4-d4bcf523f244` (the mirror) |

The operator's own reads of the plan and of the approval record while the test
ran are in the trail as well.

## Not covered by this test

- Elastic IPs and Internet Monitor monitors: no decoy of those kinds.
- A second `POST` on the decided approval (seen on 2026-11-11); only a `GET`
  was sent this time.
- The minimum age rule: the test ran with `minAgeDays` 0 on purpose. The
  weekly schedule runs with 7.
