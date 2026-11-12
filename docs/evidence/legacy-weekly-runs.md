# The first janitor: sixteen weekly runs

Before this project there was a smaller one: a single Lambda function, started
by a schedule every Monday, that swept the account, deleted what was marked as
lab and then sent an email. It was written on 2026-07-26 and replaced on
2026-11-12. It deleted exactly once: a dashboard named `LAB-janitor-selftest`,
created to try the delete path on the day the function was written. In the
sixteen scheduled runs that followed, nothing in the account carried the lab
marks, so nothing was deleted. Its snapshot, volume and address deletions never
ran.

Its stack is gone. What is left is the log group the stack was told to keep,
and this table is read from the `REPORT` line Lambda writes at the end of every
invocation. All times are UTC; 14:00 UTC is 09:00 in Bogota.

## Scheduled runs

| Monday | Started | Seconds | Memory used (MB) |
| ------ | ------- | ------- | ---------------- |
| 2026-07-27 | 14:00 | 21.5 | 220 |
| 2026-08-03 | 14:00 | 24.1 | 221 |
| 2026-08-10 | 14:00 | 23.7 | 222 |
| 2026-08-17 | 14:00 | 23.0 | 224 |
| 2026-08-24 | 14:00 | 25.4 | 223 |
| 2026-08-31 | 14:00 | 22.3 | 223 |
| 2026-09-07 | 14:00 | 26.2 | 227 |
| 2026-09-14 | 14:00 | 24.2 | 225 |
| 2026-09-21 | 14:00 | 25.0 | 226 |
| 2026-09-28 | 14:00 | 25.4 | 227 |
| 2026-10-05 | 14:00 | 22.4 | 222 |
| 2026-10-12 | 14:00 | 24.1 | 226 |
| 2026-10-19 | 14:00 | 24.9 | 223 |
| 2026-10-26 | 14:00 | 22.9 | 218 |
| 2026-11-02 | 14:00 | 23.7 | 221 |
| 2026-11-09 | 14:00 | 21.6 | 216 |

Sixteen consecutive Mondays, none missed, none failed. Durations between 21.5
and 26.2 seconds; memory between 216 and 227 MB of the 512 MB configured.

## The day it was written

Eight invocations by hand on 2026-07-26, while the function was being built:

| Started | Seconds | Memory used (MB) | |
| ------- | ------- | ---------------- | - |
| 19:04 | 10.8 | 256 | ran out of memory at 256 MB |
| 19:10 | 95.3 | 512 | one boto3 session per thread |
| 19:11 | 55.4 | 512 | |
| 19:17 | 20.5 | 219 | one shared session |
| 21:14 | 24.8 | 220 | |
| 21:15 | 17.5 | 224 | |
| 21:43 | 25.4 | 220 | |
| 21:44 | 21.6 | 225 | deleted the self-test dashboard |

The comments of that version give the cause of the first three rows: each
thread created its own session, and botocore loads every service model once
per session. [ADR 0002](../adr/0002-one-boto3-session-and-512-mb.md) keeps the
decision that came out of it.
