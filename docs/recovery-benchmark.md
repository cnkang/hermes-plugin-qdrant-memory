# Synthetic recovery and retained-storage benchmark

Status: **PASS** for the six measured sizes and synthetic correctness assertions.
Executed on 2026-10-08 against the committed release-hardening core at
`c77718edce269898db6b47088489c204cd38b234`; `git rev-parse HEAD` confirmed this
revision immediately before the run. These are local measurements of this
commit, not historical-main results or evidence of remote CI completion.

## Reproduction and measurement boundaries

```sh
PYTHONPATH=/Volumes/2T/development/hermes-agent \
  .test-env/bin/python scripts/benchmark_recovery.py \
  --sizes 0 1 100 1000 10000 100000
```

Use an environment with the plugin and actual Hermes dependencies; the
`PYTHONPATH` in the executed command locates this machine's Hermes checkout.
The script prints environment metadata and one JSON result per size.

Measured environment: macOS 27.0, Apple arm64, Python 3.14.7, SQLite 3.53.1.
Each operation has 256 ASCII text bytes and a 633-byte serialized synthetic
payload. Every case uses a disposable `TemporaryDirectory`; it neither loads
user configuration nor connects to Qdrant. Fixture admission uses one bulk
`executemany` transaction to avoid measuring 100,000 independent admissions.

Memory figures are **Python allocation peaks from `tracemalloc`, not RSS**.
Timing includes tracing overhead. Imports, fixture construction, and garbage
collection before measurement are excluded. No native SQLite cache, filesystem
cache, Qdrant allocation, embedding allocation, or process RSS guarantee can be
inferred from these figures.

The legacy comparison substitutes the former list-materializing reader into
the **same current `Runtime.recover()`**. Both actual recovery measurements
execute current commit, delete-fence lookup, metric recording, acknowledgement,
and logical payload scrubbing, using a constant-memory `FakeStore` that confirms
writes without network or embeddings. SQLite remains in WAL mode with
`synchronous=FULL`; per-operation metric and finish transactions are included
in recovery duration. This isolates reader allocation differences; it is not a
benchmark of the entire historical implementation or real-service throughput.

## Read allocation and duration

MiB means 1,048,576 bytes. Bounded scans use batches of 128 and a fixed rowid
watermark. Small-case allocation overhead can exceed list allocation; larger
backlogs plateau at about half a MiB instead of increasing with row count.

| Pending operations | Legacy scan peak MiB | Bounded scan peak MiB | Legacy scan seconds | Bounded scan seconds |
| ---: | ---: | ---: | ---: | ---: |
| 0 | 0.0027 | 0.0014 | 0.000033 | 0.000012 |
| 1 | 0.0051 | 0.0064 | 0.000039 | 0.000069 |
| 100 | 0.1667 | 0.1883 | 0.000651 | 0.000820 |
| 1,000 | 1.6369 | 0.4644 | 0.005580 | 0.007980 |
| 10,000 | 16.3524 | 0.4708 | 0.061782 | 0.079389 |
| 100,000 | 163.6338 | 0.4817 | 0.729690 | 0.795513 |

## Actual runtime recovery

| Pending operations | Legacy reader recovery peak MiB | Bounded recovery peak MiB | Legacy reader recovery seconds | Bounded recovery seconds | Terminal reopen peak KiB |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 0.0037 | 0.0011 | 0.000056 | 0.000021 | 6.62 |
| 1 | 0.0171 | 0.0196 | 0.000457 | 0.000521 | 7.93 |
| 100 | 0.2078 | 0.2249 | 0.026115 | 0.025662 | 7.89 |
| 1,000 | 1.6781 | 0.4860 | 0.278215 | 0.292396 | 7.87 |
| 10,000 | 16.3946 | 0.4968 | 3.001698 | 3.002963 | 7.85 |
| 100,000 | 163.6766 | 0.4964 | 30.675616 | 29.859949 | 7.82 |

At 100,000 operations, the measured recovery allocation peak decreased by
99.697%. Bounded recovery was about 2.7% faster in this run; a single run does not
establish a speed improvement. Payload size and configured batch size still affect the
bounded peak. A backlog of maximum-size payloads requires more memory than this
633-byte fixture even though allocation is bounded by batch size.

All cases asserted exact confirmed write counts, zero recovery failures, zero
non-COMMITTED operations, and zero retained terminal payloads. Reopening the
100,000-terminal-row ledger took 0.003657 seconds and allocated 8,004 Python
bytes at peak. These fixtures contain no migration manifests: memory required
by retained, unfinished migration manifests is a separate capacity consideration.
Ordering, failures, concurrent admission, stop/restart, and isolation are covered
by behavioral regression tests; this benchmark uses distinct UPSERT points and
does not replace those tests.

## SQLite query behavior

The actual query planner reported:

```text
Legacy:
SEARCH operations USING INDEX operations_by_collection_status (collection=? AND status=?)

Bounded:
SEARCH operations USING INDEX operations_by_collection_status (collection=? AND status=? AND rowid>? AND rowid<?)
```

Both scans use the existing collection/status index; neither plan uses a
temporary sort. The bounded reader fetches at most 128 rows per query using
rowid ranges, without OFFSET. It closes its cursor and releases the ledger lock
before yielding work to the runtime. The fixed upper watermark excludes new
admissions from the existing scan. A fresh recovery round obtains a new watermark.

## Physical storage and privacy boundary

For the 100,000-operation bounded case:

| Measurement | Before recovery | After recovery and scrub | After close and reopen |
| --- | ---: | ---: | ---: |
| Database file bytes | 138,493,952 | 138,539,008 | 138,539,008 |
| WAL file bytes | 139,383,752 | 139,383,752 | 0 |
| SHM file bytes | 294,912 | 294,912 | 32,768 |
| SQLite page count | 33,812 | 33,823 | 33,823 |
| SQLite freelist pages | 0 | 1 | 1 |
| Retained terminal payloads | Not terminal yet | 0 | 0 |

Recovery scrubbed terminal payloads logically but did not shrink the database.
In this run the main file remained about 132.1 MiB, with a roughly 132.9 MiB WAL
before connection close. WAL automatic checkpointing/reuse can retain a large
file during an active connection. Closing the final connection checkpointed and
removed its WAL; reopening created fresh SQLite sidecars. These are measured
filesystem behaviors, not a secure-erasure guarantee.

Terminal operation identities and delete-fence metadata remain necessary for
idempotency and preventing stale replay. Completed manifests retain migration
audit/resume metadata; pending/failed payloads remain necessary for recovery.
This change performs no automatic history deletion or retention compaction.
Operators should provision and monitor database plus WAL/SHM space, account for
backups and snapshots, and use the maintenance guidance in
[operations](operations.md) and the privacy boundaries in
[security](security.md). Logical scrub and checkpointing cannot erase historical
backups, filesystem snapshots, or all old SQLite pages.
