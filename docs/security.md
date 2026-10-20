# Security and deployment boundaries

Explicit shared CLI policy paths are confined to the invocation directory after
canonicalization, including symlink and parent-segment resolution. Start the
Owner from its private directory (or a deliberately chosen enclosing directory).
This follows the existing maintenance file boundary and rejects an outside
policy before it is read. Programmatic policy loading remains a trusted operator
interface and never exposes policy contents through client tools or diagnostics.

For the optional protected Shared Owner, see the [versioned threat model](shared-memory/threat-model-v1.md)
and [deployment security and diagnostics](shared-memory/deployment-security.md).
Its account-local claim does not fence remote database writers. Direct Qdrant
writes bypass Ledger and application revision checks. Effective database key
scope remains UNVERIFIED in the doctor, even when a connection succeeds.

This guide covers the Hermes memory plugin and its optional standalone CLI/MCP
interfaces. For vulnerability reporting, see [Security Policy](../SECURITY.md).
For configuration and recovery, see [configuration](configuration.md),
[operations](operations.md) and [standalone setup](standalone.md).

## Credentials and network access

The Hermes plugin resolves credentials through the host's profile-aware
`get_secret()`. Explicit Qdrant JSON credentials take precedence for compatibility.
Protect configuration files containing them. Extraction and relation requests use
Hermes's `PluginContext.llm`, and model/provider overrides require the operator's
Hermes trust grants. The standalone runtime uses its own operator-configured
profile and embedding credentials. It does not require Hermes.

Use a Qdrant **Database API Key** limited to the destination collection's required
permissions. Give migration credentials read-only source access where possible,
and revoke temporary credentials after verification. Use HTTPS for Qdrant Cloud
and rotate keys. Endpoint URLs must not contain inline credentials, query strings
or fragments. See [Qdrant security guidance](https://qdrant.tech/documentation/security/).

Secrets can be injected by a launcher such as
`bws run --project-id ... -- hermes`. The plugin does not invoke Bitwarden or read
a vault. Injected values and existing shell environment values are indistinguishable
inside the process. The launcher controls their precedence.

For CI deployments using Cloud credentials, restrict the `qdrant-cloud`
environment to approved refs and require approval where appropriate. The optional
Cloud workflow runs on `main` pushes and manual dispatch. A manual dispatch can
select another ref. Workflow triggers alone do not protect credentials.

## Private storage and retention

On POSIX systems, state directories use mode 0700, and SQLite state and saved
configuration use mode 0600. Windows relies on inherited filesystem ACLs. Protect
the profile with a private Windows ACL. Protect `state.db`, its WAL/SHM sidecars,
configuration, snapshots and backups as sensitive data.

The ledger may contain raw conversations and prepared memory payloads. Committed
event bodies and committed/superseded operation bodies are logically scrubbed,
except operations needed by an incomplete migration. Pending/failed payloads
remain available for recovery. Identity/status rows, deletion fences, migration
manifests, author provenance and history quarantine have no time-based expiry.
Do not manually prune these records: recovery relies on them.

Deleting a Qdrant point or resetting a destination does not securely erase SQLite
pages, WAL files or copied backups. Use storage encryption and a backup retention
policy appropriate to the data. Error reporting includes exception type and
retryability rather than API keys or remote response bodies.

### Deployment checklist for private state

- Place the entire profile, embedded collection and backup destination on
  encrypted storage. Include SQLite WAL/SHM sidecars in the protected boundary. Encrypting only an exported file does not protect the live ledger.
- On Windows, provision a private ACL before initialization and check inherited
  permissions on the profile, model cache and backups. POSIX modes do not
  establish a Windows access policy.
- Define a backup expiry period appropriate to the data and apply it to all
  copies. Take a consistent backup after stopping every writer, or use a
  supported database backup mechanism. Copying a live SQLite file alone can
  omit admitted work.
- Monitor pending/failed work and storage growth. Resolve recovery through the
  supported CLI before any lifecycle cleanup. Never delete ledger rows, deletion
  fences, locks or sidecars to implement a retention schedule.
- For data disposal, account for encrypted-volume snapshots and off-host copies.
  A scoped delete or reset is a logical operation, not secure erasure.

The plugin does not manage volume encryption keys or automatically expire
recovery records. Those policies belong to the deployment operator.

## Identity, scope and shared sessions

Recall and exact-ID memory tools enforce their configured user/agent scope.
Hermes gateway authors take precedence for their turns. Bot turns cannot recall
or use personal memory tools, including bots without an author ID. Unidentified
turns in a session with gateway author history also fail closed. Ordinary CLI
turns without author metadata keep their configured profile scope.

Bot and non-primary turns do not automatically create memories. A completed human
turn with a captured explicit author can still be persisted when delivered
asynchronously after a bot transition. Bot transitions invalidate cached and
in-flight recall.

Whole-transcript supplementary extraction requires complete single-author
history matching the current scope. After restart, a failed author callback or
previously unseen history skips session-end extraction. Pre-compression
evidence is archived without extraction until the host explicitly starts or
resets a fresh transcript. Explicitly attributed per-turn writes remain available.

Bot or unidentified participation also quarantines supplementary extraction.
Author history and quarantine persist across restarts and rewinds. Compression
and branch continuations inherit them. An explicit authorized transcript reset
establishes fresh history. Unattributed checkpoints use a neutral `__mixed__`
user marker and null agent identity. These protections do not automatically
remove memories created by earlier versions.

Returning to a known session without a new `user_id` preserves its recorded
scope. An explicit user identity or reset can replace it. A new unrelated session
uses the default scope. Migration preserves source scopes, including null agent
identity. Native memory tools and MCP tool arguments cannot override scope.
The operator CLI can select maintenance scopes, so grant it only to trusted
operators. Treat recalled text as untrusted data, not agent instructions.

## Durability and writer ownership

Turn durability begins when the provider commits the callback's event to its
ledger. Hermes submits `sync_turn` through an in-memory background queue. A host
crash or bounded shutdown can lose a callback that has not reached the ledger.
Restart replay covers admitted work. Pre-compression checkpoints durably archive
host-filtered direct user/assistant evidence before compression.

The local OS lease excludes cooperating writers sharing one profile destination,
including a retiring worker. Different profile homes do not share that lease,
even if they target the same collection. Run only one writer per destination
across all profiles and hosts, and stop all writers before maintenance. The lease
does not provide distributed coordination or make shared absolute storage paths
safe.

Scoped deletion preserves fences and explicit versioned deletion intents so that
older admitted work cannot recreate deleted content. A later event can explicitly
add it again. A literal agent ID `*` is distinct from an all-agents deletion.
Ambiguous legacy intents require explicit operator resolution. See
[operations](operations.md).

## Filesystem access and MCP limits

The standalone CLI confines explicit `--source-json`, `--source-config` and
`--output` paths to the invocation directory after resolving parent traversal
and existing symlinks. Choose that working directory intentionally and keep its
contents under trusted control. Legacy `hermes qdrant-memory` and direct Python
maintenance calls retain trusted-operator file access, including external paths.

Path validation is not an OS sandbox and does not prevent concurrent filesystem
mutation. For untrusted shared directories, use an external sandbox or an
equivalent filesystem access policy. Do not allow another process to replace
files or directories during maintenance.

The optional MCP server runs over local stdio and exposes only scoped search,
add, update and delete. It has no migration, source-file or export tools and
rejects additional tool arguments. Its profile, credentials and identity are
chosen by the operator launching it. See the [MCP guide](mcp.md) for connection
and shared-memory boundaries.
