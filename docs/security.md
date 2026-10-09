# Security

Terminal identity rows and delete fences are deliberately retained. Do not prune
them or completed migration manifests to reduce replay memory: they preserve
deletion intent, idempotency and resume evidence. Logical payload scrubbing is not
secure erase. The deployment must enforce encryption and backup expiry.

The plugin resolves credentials through Hermes's profile-aware `get_secret()`. Explicit
Qdrant JSON credentials have higher precedence for compatibility. LLM credentials
never enter this plugin: extraction and relation calls use `PluginContext.llm`.
Provider/model overrides require the operator's Hermes trust grants.

Use `bws run --project-id ... -- hermes` for Bitwarden injection. BWS and shell
environment values are indistinguishable inside a process. Configure the launcher
to fill only missing variables if that distinction matters. This plugin does not
shell out to BWS or read a vault. It introduces no behavior environment variables.

Use a Qdrant **Database API Key**, scoped to the target collection's read/write
permissions. A migration key may temporarily have read-only source access. Revoke
it after verification. Use HTTPS for Cloud and rotate expiring keys. See
[Qdrant security guidance](https://qdrant.tech/documentation/security/). The
credentialed Cloud smoke workflow runs only on pushes to `main` and manual
dispatch, never on `codex/**` branch pushes. This protects repository Cloud
secrets from arbitrary branch code.

On POSIX systems, state directories use mode 0700, and SQLite state and saved config
use mode 0600. Windows uses inherited filesystem ACLs; Python's `chmod` mode bits do
not establish an equivalent private ACL. Operators must protect the Hermes profile
directory with an appropriate Windows ACL.
The durable ledger can contain raw conversation events and prepared memory payloads.
It logically scrubs committed event bodies and committed/superseded operation bodies,
except operations still needed by an incomplete migration manifest. Identity/status
rows and manifests remain. Pending/failed payloads remain for recovery. Ledger rows
and manifests have no time-based expiry. Deleting a Qdrant point does not clear its
ledger identity or pending/failed work. A scoped content-hash tombstone prevents an
older admitted event from recreating the exact deleted content under a different point
ID. A later event may explicitly add it again. Scoped deletions record
explicit versioned intents. A literal agent id `*` and an all-agents deletion
are never conflated. An ambiguous pre-versioned intent is refused, not
guessed. Treat `state.db`, WAL/SHM sidecars, snapshots
and backups as sensitive conversation data. A destination reset clears current rows
but is not secure erasure from SQLite pages/WAL or copied backups. The plugin never
logs or persists API keys and response bodies as errors. It reports only the
exception type and retryability.
The plugin refuses endpoint URLs that contain inline credentials, query strings or
fragments.

Turn durability begins when Hermes invokes the provider callback and the provider
commits the event to the plugin ledger. Hermes currently submits `sync_turn` via
an in-memory
background queue, so a host crash or bounded shutdown may lose a not-yet-admitted
callback. The plugin's restart replay covers work already admitted to its ledger.

Checkpoint v2 archives host-filtered direct user/assistant evidence in that same
private ledger. The plugin retains mixed-author evidence without attributing
extraction to the latest speaker. A multi-author checkpoint stores a non-attributed `__mixed__`
scope marker with a null agent identity instead of any one speaker's scope, while a
single-author checkpoint keeps that author's real scope. A local OS lease excludes
simultaneous cooperating processes for one profile destination, including a retiring
worker. Lease files are local to `HERMES_HOME`. Different profile homes on the same
machine do not share the lease even when they target the same remote collection.
Deploy only one writer per destination across all profiles and hosts. It is not
distributed coordination and does not make explicitly shared absolute storage paths safe.
Stop all writers across hosts before maintenance.

Every recall and exact-ID tool operation enforces user/agent scope. A gateway
author takes precedence for their turn. Bot turns cannot recall or invoke personal
memory tools, including bots without an author ID. An unidentified turn in a session
with gateway author history also fails closed. Ordinary CLI turns without author
metadata retain their configured profile scope. Bot and non-primary turns do not
automatically write memories. The plugin ignores builtin notifications during a
blocked turn. Completed human turns carrying an explicit author can still persist if
delivered asynchronously after a bot transition.

Bot or unidentified shared-session participation quarantines supplementary
session-end/checkpoint extraction, even after a human resumes. Checkpoints remain
durably archived under the neutral `__mixed__`/null-agent scope without extraction.
The private ledger persists the quarantine, and it survives provider restart and
session rewind. Compression and branch continuations inherit the parent's scope,
author history, active denial, and durable quarantine. An explicit transcript reset
clears it once the current turn is authorized. Bot transitions invalidate cached
and in-flight recall generations.
The plugin does not automatically remove existing memories created before this
protection.

Switching back to a known session without a
`user_id` preserves that session's previously recorded author scope. The plugin
overwrites the scope only on `reset=True` or an explicit `user_id`, and a brand-new session
without a parent still falls back to the default scope. Migration preserves source
scopes, including null agent identity. Tools cannot supply a scope override. Recalled text is
untrusted data and never becomes executable plugin instructions.
