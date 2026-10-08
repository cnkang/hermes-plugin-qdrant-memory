# Migrating Mem0

Keep the source collection unchanged and use a separate target collection.
Back up the export and configuration. Use the same active Hermes profile for setup,
migration and verification. Stop all sessions/gateways writing to the target before
the real migration in **every deployment mode**, including Server and Cloud.
Embedded Qdrant also requires stopping other clients that hold its local storage lock.

## JSON export: plan, stop writers, migrate and restart

Replace `/path/export.json` with your export's absolute path and
`hermes_qdrant_memory` with the intended target collection throughout these commands.
The target must match the plugin configuration if the agent should use it afterward;
`--target-collection` overrides only the migration command, not saved configuration.

```bash
hermes qdrant-memory status
hermes qdrant-memory migrate mem0 --source-json /path/export.json \
  --target-collection hermes_qdrant_memory --dry-run

# For a gateway installed as a background service:
hermes gateway status
hermes gateway stop
hermes gateway status

# Exit interactive Hermes sessions with /exit. For a foreground gateway,
# press Ctrl-C in its terminal and wait for the process to exit.
hermes qdrant-memory migrate mem0 --source-json /path/export.json \
  --target-collection hermes_qdrant_memory --reembed --resume --verify
hermes qdrant-memory verify --collection hermes_qdrant_memory
hermes qdrant-memory stats --collection hermes_qdrant_memory
```

Only after migration and verification succeed, restart the background service if it
was running before maintenance:

```bash
hermes gateway start
hermes gateway status
```

If you normally run the gateway in the foreground, restart it with `hermes gateway run`
instead of `gateway start`. For CLI-only use, start a new session with `hermes`.
Stopping a shared gateway can interrupt other routed profiles; coordinate its downtime.
Writers on other machines are not covered by the plugin's local writer lock and must
also be stopped before maintenance.

`--dry-run` validates and plans the source without opening the target collection or
ledger. It may succeed while an agent is running; this does not prove that the real
migration can acquire the writer lock or that the target services are ready.

## Writer lock conflict and interrupted migration

Migration prints live progress to **stderr** by default, while stdout remains the
final JSON result. Phase messages cover source loading, target/embedding
initialization, target comparison, embedding/writing pending records, and
`--verify`. Record counters use the total for that phase; writing counts only
pending operations and advances after a batch is durably committed. Resume may
therefore have fewer pending writes than source records. Updates are throttled;
phase changes and final counts are always shown.

During a slow service request or retry, a waiting line appears every 10 seconds
without new output, showing the current phase, elapsed time, and time since the
last progress update. This confirms that the CLI reporter is alive, but does not
prove that the service request is advancing. No source text, record identifiers,
paths, or credentials are included in progress lines. Use `--quiet` to suppress
progress, or redirect stdout to save JSON while keeping progress visible:

```bash
hermes qdrant-memory migrate mem0 --source-json /path/export.json --verify > migration-result.json
```

`WriterBusyError` / `code: writer_busy` means another local session or gateway still
owns the target's writer lock. Migration has not started writing target records.
Use `hermes gateway status` and `hermes gateway list` to locate running gateways,
stop the relevant service with `hermes gateway stop`, and exit other sessions as above.
Wait for shutdown to finish: an in-flight request may retain the lock until its worker
exits. If a supervisor restarts the process, stop it through that supervisor.
Do not delete `.writer.lock`, `state.db` or its SQLite sidecars to bypass ownership.

Once the writer has stopped, rerun the original source and target:

```bash
hermes qdrant-memory migrate mem0 --source-json /path/export.json \
  --target-collection hermes_qdrant_memory --resume --verify
```

For a migration that actually started but was interrupted or retained failed operations,
correct the connection/configuration problem first, then resume the same export,
target and embedding pipeline while writers remain stopped:

```bash
hermes qdrant-memory migrate mem0 --source-json /path/export.json \
  --target-collection hermes_qdrant_memory --resume --retry-failed --verify
hermes qdrant-memory verify --collection hermes_qdrant_memory
hermes qdrant-memory stats --collection hermes_qdrant_memory
```

Do not clear the collection to resolve a lock conflict. Counts alone do not prove a
successful import; require successful verification before restarting the agent.

## Supported sources and verification

JSON input may be an array, an object with `memories`, `results` or `data` array,
or an ID-to-record mapping. Conflicting duplicates are refused so an export merge
cannot silently choose one version. Missing timestamps map to the Unix epoch for
reproducible supplemental imports. Unknown fields survive in
`metadata.legacy_mem0_extra`; legacy metadata and structured attributes have their
own namespaces. `_cloud_memory_id` takes priority over local point IDs.

```bash
hermes qdrant-memory migrate mem0 \
  --source-qdrant-collection hermes_mem0 --source-config /path/mem0.json \
  --target-collection hermes_qdrant_memory --reembed --resume --verify
```

Only this source adapter reads the legacy `vector_store.config` block. It calls
`scroll` with payloads and never writes source points or indexes. If the source
is configured at the same endpoint as the target, the collection names must differ.
There is no Mem0 Cloud API client and no quota-consuming export step in this plugin.

Source IDs are mapped to UUID5 with user and agent scope. Semantic deduplication
is disabled during migration: 1000 unique IDs produce 1000 logical target records
even when all texts match. Supplemental changed records update the same UUID;
unchanged complete payloads skip. Every migration saves a manifest in the private
SQLite ledger with source checksum/plan hash, pipeline identity, per-record IDs,
payload hashes, operation keys, counts and timestamps.

`--resume` selects the same source snapshot, collection and pipeline. A changed
source snapshot produces a fresh plan, permitting supplemental imports.
`--retry-failed` retries only prepared operations belonging to that manifest.
`--verify` checks each expected point ID, caller scope and payload hash; count
alone cannot certify a migration. `verify` separately checks payload hashes and
vector dimensions across the collection using exact counts.

Delete fences compare migration operations by their durable ledger admission time,
not the source record's preserved `updated_at`. A fresh import after a scoped
delete can intentionally restore that content, including records without source
timestamps. Operations prepared before the delete remain fenced on replay.

`SUPERSEDED` means that a later mutation or explicit deletion invalidated a prepared
write. It is terminal work, not a successful write. Migration summaries distinguish
processed records from applied ADD/UPDATE records and superseded records. `applied`
counts operations actually acknowledged COMMITTED, including a write later deleted;
it is not a claim that those points still exist. `superseded` identifies invalidated
plan records and can overlap historical applied/skipped counts. An operation fenced
before it committed never increments applied/added/updated. A settled
manifest can contain superseded work; exact snapshot verification still reports
the conflict and cannot certify the deleted source record as present.
Repeated `--resume --retry-failed` must preserve that result rather than restoring
deleted content. Review the conflict before starting a deliberately new import
without `--resume`; a new import explicitly authorizes planning against current
target state. Source timestamps, including future timestamps, do not grant that
authorization to an old operation. Changed source snapshots create a fresh plan.

With `--verify`, a superseded plan raises the sanitized JSON error
`migration_superseded` (`retryable: false`) and exits nonzero. The message explains
that resume will not restore deleted memories; source text and IDs are not exposed.
Without verification, a settled plan can report completion with superseded records;
that is not certification of the original snapshot. While failed or missing records
remain, `--verify` raises `migration_incomplete` (`retryable: false`) instead, and
progress reports `Migration incomplete` first. Correct the failure and use
`--resume --retry-failed --verify`, then inspect any remaining conflict before
authorizing a fresh import. Do not use a fresh import merely to hide an unexplained
failure or deletion conflict.

Re-embedding is the default. `--reuse-vectors` is reserved, not implemented in 0.1,
and refused because supported
Mem0 inputs do not expose a trusted pipeline fingerprint and metric contract.
Text oversize defaults to rejection; `--oversize truncate` explicitly opts into
UTF-8-safe truncation with a payload marker. Metadata and total payload oversize
remain errors. Invalid input is rejected during planning before target writes.

If the process exits after an upsert but before ledger acknowledgement, resume
replays the prepared UUID upsert. It cannot create another logical point. If a
later update supersedes an older failed operation, retries preserve the later
state. Return to Mem0 by setting `memory.provider` to `mem0` and restarting; the
source collection remains intact.

Source and target isolation compares physical endpoint/path and collection together.
Matching collection names on different destinations are permitted; the same
canonical endpoint/path and collection are refused. Resumed SKIPs are rechecked for
open operations and current payload digest; stale SKIPs trigger a fresh manifest.

Legacy host/port-only source configuration defaults to HTTPS for remote hosts
and HTTP for loopback. An explicit source URL preserves the operator's configured
transport; use TLS for remote deployments.
