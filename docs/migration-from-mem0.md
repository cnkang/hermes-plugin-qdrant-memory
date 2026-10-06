# Migrating Mem0

Keep the source collection unchanged and use a separate target collection.
Back up the export and configuration. Stop the agent before accessing an embedded
store; maintenance commands do not coordinate Qdrant's exclusive local lock.

```bash
hermes qdrant-memory migrate mem0 --source-json /path/export.json --dry-run
hermes qdrant-memory migrate mem0 --source-json /path/export.json \
  --target-collection hermes_qdrant_memory --reembed --resume --verify
hermes qdrant-memory verify
```

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

Re-embedding is the default. `--reuse-vectors` is refused in 0.1 because supported
Mem0 inputs do not expose a trusted pipeline fingerprint and metric contract.
Text oversize defaults to rejection; `--oversize truncate` explicitly opts into
UTF-8-safe truncation with a payload marker. Metadata and total payload oversize
remain errors. Invalid input is rejected during planning before target writes.

If the process exits after an upsert but before ledger acknowledgement, resume
replays the prepared UUID upsert. It cannot create another logical point. If a
later update supersedes an older failed operation, retries preserve the later
state. Return to Mem0 by setting `memory.provider` to `mem0` and restarting; the
source collection remains intact.

Legacy host/port-only source configuration defaults to HTTPS for remote hosts
and HTTP for loopback. An explicit source URL preserves the operator's configured
transport; use TLS for remote deployments.
