> **Version boundary:** this page describes the main branch after the local
> Ledger write watermark change. The published Runtime 0.7.1 wheel does not
> include that guard. For a 0.7.1 installation, use the
> [published 0.7.1 guide](upgrade-0.7.1.md).

# Adopt a Qdrant policy with the upgrade commands

Runtime 0.7.1 separates three things that used to be implicit:

- the **target** policy derived from the profile configuration;
- the **effective** policy this destination accepted;
- the **observed** state of the collection, including payload indexes.

`upgrade plan` reads the destination and saves one plan. `upgrade apply` executes
that plan inside a writer lease. `upgrade resume` continues an interrupted plan.
`upgrade status` reads the saved plan only.

## Before you start

1. Stop every Hermes gateway, provider worker and MCP server that writes this
   profile. For a remote destination also stop every other Owner, device and
   credential that can write the same collection.
2. Confirm the profile configuration, collection name and embedding pipeline.
   An in-place upgrade keeps all three. A different embedding fingerprint is a
   blocker, not a migration.
3. Keep the current Runtime release available for an application rollback.

## Plan

```sh
qdrant-memory --home /path/to/profile upgrade plan
```

The result carries:

| Field | Meaning |
| --- | --- |
| `plan_id` | Identifier `apply` and `resume` must reference |
| `source_policy.status` | `missing`, `accepted`, `pending` or `corrupt` |
| `source_policy.effective` | Effective policy, `legacy` when no record exists |
| `target` | Desired policy, including the index policy version |
| `indexes.status` | `equivalent`, `missing`, `conflict` or `not_applicable` |
| `indexes.entries` | Per-field expected and observed index type |
| `indexes.extra` | Indexes outside the policy, reported and preserved |
| `checkpoint` | Memory count, identity sentinel and vector schema |
| `backend_version` | `server-info`, `embedded-engine` or `unavailable` |
| `blockers` | Conditions that make this destination ineligible |
| `warnings` | Conditions to review that do not block the apply |

`plan` writes the plan record under the profile. It does not write the Ledger,
create an index, run recovery, change configuration or download a model. Use
`--dry-run` to read the same result without saving it.

A plan that already started and has not reached `ready` keeps its phase,
failure, step records and recovery point. A normal `plan` reports that record,
its `plan_id` and the next command instead of overwriting it. Resume the saved
plan. If the plan is stale and cannot continue, use `upgrade plan --replace`.
The replacement archives the old plan and its backup reference under the profile
before it saves a new plan. Do not delete a control-plane file by hand.

An embedded destination needs exclusive access to the local store. If another
writer holds the lease, the command stops with `writer_busy` and no state
changes.

## Apply

```sh
qdrant-memory --home /path/to/profile upgrade apply --plan PLAN_ID
```

Embedded destinations need no extra flag. The writer lease is the isolation
boundary and the local recovery point is created automatically.

Remote destinations need both flags:

```sh
qdrant-memory --home /path/to/profile upgrade apply --plan PLAN_ID \
  --confirm-writers-isolated --accept-operator-backup
```

`--confirm-writers-isolated` states that every other writer of that destination
is stopped. `--accept-operator-backup` states that you hold a provider-side
snapshot taken at the same stopped-writer point. Runtime does not hold Qdrant
administrative backup rights and never silently skips the recovery point.

For an enrolled destination pass the same protected policy file that other
maintenance commands accept with `--policy`.

### Steps

| Step | Effect |
| --- | --- |
| `backup` | Embedded: copy the closed store, Ledger and policy records, then reopen the copy. Remote: record the operator attestation |
| `indexes` | Create missing managed indexes, then re-read the schema |
| `verify` | Re-read indexes, identity, point count and run a scoped filter on real data |
| `activate` | Write the effective policy at a new generation |

Each step is idempotent. A crash after a step succeeded but before its record
was written is detected on `resume` instead of repeating destructive work. If
the embedded backup copy exists but its step record was lost, `resume` verifies
the copy against the planned point count. It then adopts the existing path.
It does not create a second recovery point.

`apply` and `resume` take the writer lease before they read the saved plan
again. If another pass replaced the plan while the lock was acquired, the older
plan is refused and the newer record is not overwritten. Repeating a finished
plan reports `already_applied` without a new backup or policy generation.

## Resume

```sh
qdrant-memory --home /path/to/profile upgrade resume --plan PLAN_ID
```

`resume` accepts the same flags as `apply`. It re-validates the saved plan
against the live profile. A changed configuration, collection, destination or
point count is a stale plan. Use `upgrade plan --replace` to keep the old
recovery record and plan again. Do not force the old plan.

The plan also records a local Ledger write watermark when it is created under
the writer lease. Before `apply` or `resume` changes anything, the same lease is
taken and the current watermark is compared with that record.

- If no local Runtime write was committed after the plan, resume keeps the same
  plan ID and the original backup. Repeating a completed resume does not create
  another backup or increase the policy generation.
- A completed destination reset advances the Ledger generation recorded in the
  watermark, so a plan from before the reset is stale even if its rowid values
  later repeat.
- If a local Runtime write was committed, or the saved plan has no watermark,
  the plan is stale. Keep writers isolated, run `upgrade plan --replace`, take a
  new backup at the new stopped-writer point, then apply. The old plan and its
  backup reference are archived. Do not treat the old backup as a new
  checkpoint, and do not edit or delete it.
- Status reads and other read-only access do not change the watermark. Local
  Runtime writes do. A watermark match does not prove every payload is
  byte-for-byte unchanged; it proves that this profile's local write ledger did
  not advance. An external writer, another host or a Cloud credential can still
  write without touching this local Ledger.

A successful resume is not evidence that the stop-write boundary stayed closed.
If you cannot confirm that every writer stayed stopped, replace the plan instead
of resuming it. For Cloud and Server destinations, take the new backup through
the provider at the new boundary.

After taking the lease, `apply` and `resume` read the Shared Owner state.
An admitted lifecycle receipt whose event has not reached a terminal state
blocks the pass. So does an incomplete shared request. Complete the existing
Owner recovery flow first: restart the Owner so its lifecycle recovery
re-enqueues the admitted plan. If the event is terminally failed, stop the
Owner and run the operator `retry` against its profile before the restart.
Maintenance commands never clear receipts, re-extract a turn or replay a pending
event for you. After recovery, resume the plan when it is still valid, or use
`upgrade plan --replace` when it is stale.

## Status

```sh
qdrant-memory --home /path/to/profile upgrade status
```

`status` reads the control plane only. It never opens the destination, so it is
safe while a server is unreachable. `unplanned` means no plan exists. `planned`
means a plan is ready to apply. `applying` and `verifying` mean a pass stopped
part way. `failed` carries a sanitized failure type. `ready` means the effective
policy is committed. `backup_path` reports a recovery point that is still
recorded, and `plan_stale` reports whether the saved plan still matches the
profile. `plan_stale_reason` is `destination`, `write_since_plan` or
`no_watermark`. `write_watermark` reports the current local Ledger boundary:
`max_rowid`, committed count and reset epoch. `next` distinguishes apply,
resume and `plan --replace`.

## Blocker reference

| Code | Action |
| --- | --- |
| `not_initialized` | Run the normal `init` first. Upgrade never creates a collection |
| `fingerprint_mismatch` | Keep the current environment. Export and re-embed into a new profile and collection |
| `dimension_mismatch`, `distance_mismatch`, `vector_datatype` | Not an in-place upgrade. Choose a compatible collection |
| `identity_missing` | The collection has no trusted fingerprint. Do not adopt it in place |
| `index_conflict` | Inspect the field type. Runtime never deletes or rebuilds a conflicting index |
| `pending_operations`, `failed_operations`, `failed_events`, `quarantined_events` | Clear the work with the existing recovery path first. A failed local write may have reached the destination |
| `reset_intent`, `scope_delete_intent`, `shared_pending` | Resume or resolve the destructive intent first |
| `shared_owner_receipt` | Restart the Owner so its lifecycle recovery re-enqueues the admitted plan. For a terminally failed event, run the operator `retry` before the restart. Do not clear the receipt by hand |
| `plan_stale` | Local writes crossed the saved stop-write boundary, or the plan has no watermark. Keep writers isolated, use `upgrade plan --replace`, then take a new backup and apply. Do not reuse the old recovery point |
| `backend_unreachable` | Fix connectivity and permissions, then plan again |
| `policy_corrupt` | The saved policy record is unreadable. Inspect it before any apply |

## Rollback boundaries

- **Application rollback** is supported. Reinstall the previous Runtime release.
- **Storage downgrade** is not supported. Do not open data written by a newer
  server or embedded client with an older one.
- **Point-in-time restore** is supported through the recovery point. It discards
  writes that landed after that point.
- **Switching back to an old writer** after new writes landed is not a rollback.
  The old writer is missing those writes.

## What the lease does not cover

`WriterLease` excludes cooperating local writers of one profile and destination.
It does not block a process on another machine, an older client that does not
cooperate, or a different credential writing the same Cloud collection. That is
why a remote apply needs an explicit isolation confirmation.
