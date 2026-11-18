# 0002. One boto3 session, cached clients and 512 MB

## Status

Accepted on 2026-07-26, kept on 2026-11-02 for the detector and the executor.

## Context

The detector asks five services in every enabled region, 25 regions in the
owner's account, plus the global ones, with regions swept in parallel threads. botocore
loads the model of each service once per session. The first janitor created a
session per thread: its first run, at 256 MB, ran out of memory, and the next
two filled 512 MB and took 55 and 95 seconds. Lambda also gives CPU in
proportion to memory.

## Decision

One `boto3.Session` per invocation and one client per service and region, kept
in a cache guarded by a lock so two threads never build the same client. Every
client shares one configuration: connect timeout 5 seconds, read timeout 20,
standard retries, three at most. The detector and the executor get 512 MB;
the notifier and the approval handler, which make a handful of calls, get 256.

## Consequences

- With one session, the same sweep used about 220 MB and took about 20
  seconds, the same day. The sixteen scheduled runs that followed, at 512 MB,
  took 21 to 27 seconds and used 216 to 227 MB. Half the memory is headroom,
  and it buys the CPU that makes the sweep fit in seconds.
- A hung endpoint costs 20 seconds per attempt, not the function's whole
  timeout.
- The cache lives for one invocation. Nothing is shared between runs, so a
  stale credential or endpoint cannot outlive one.
- Rejected: one client per call (simple, several times slower) and a client
  cache at module level (faster on warm starts, but a weekly job is always a
  cold start).

## Where to look

- [src/shared/aws_clients.py](../../src/shared/aws_clients.py): `ClientFactory`.
- [lab_janitor/detector.py](../../lab_janitor/detector.py) and
  [lab_janitor/executor.py](../../lab_janitor/executor.py): `memory_size=512`.
- [docs/evidence/legacy-weekly-runs.md](../evidence/legacy-weekly-runs.md):
  duration and memory of each run.
- [tests/unit/test_aws_clients.py](../../tests/unit/test_aws_clients.py):
  `test_client_config_and_cache`.
