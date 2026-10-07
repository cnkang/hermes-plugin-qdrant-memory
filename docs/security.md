# Security

Credentials are resolved through Hermes's profile-aware `get_secret()`. Explicit
Qdrant JSON credentials have higher precedence for compatibility. LLM credentials
never enter this plugin: extraction and relation calls use `PluginContext.llm`.
Provider/model overrides require the operator's Hermes trust grants.

Use `bws run --project-id ... -- hermes` for Bitwarden injection. BWS and shell
environment values are indistinguishable inside a process; configure the launcher
to fill only missing variables if that distinction matters. This plugin does not
shell out to BWS or read a vault. It introduces no behavior environment variables.

Use a Qdrant **Database API Key**, scoped to the target collection's read/write
permissions. A migration key may temporarily have read-only source access; revoke
it after verification. Use HTTPS for Cloud and rotate expiring keys. See
[Qdrant security guidance](https://qdrant.tech/documentation/security/). The
credentialed Cloud smoke workflow is triggered only by pushes to `main` and manual
dispatch, never by `codex/**` branch pushes, so repository Cloud secrets are not
exposed to arbitrary branch code.

State directories are private (0700), SQLite state and saved config are 0600.
The durable ledger contains raw conversation events and prepared memory payloads;
treat backups as sensitive. API keys and response bodies are never logged or
persisted as errors: only exception type and retryability are reported. Endpoint
URLs containing inline credentials, query strings or fragments are refused.

Checkpoint v2 archives host-filtered direct user/assistant evidence in that same
private ledger; mixed-author evidence is retained without attributing extraction
to the latest speaker. A multi-author checkpoint stores a non-attributed `__mixed__`
scope marker with a null agent identity instead of any one speaker's scope, while a
single-author checkpoint keeps that author's real scope. A local OS lease excludes
simultaneous writers for one profile destination, including a retiring worker. It does not coordinate writers
on separate machines or make explicitly shared absolute storage paths safe.

Every recall and exact-ID tool operation enforces user/agent scope. A gateway
author takes precedence for their turn. Bot turns cannot recall or invoke personal
memory tools, including bots without an author ID. An unidentified turn in a session
with gateway author history also fails closed; ordinary CLI turns without author
metadata retain their configured profile scope. Bot and non-primary turns do not
automatically write memories, and builtin notifications during a blocked turn are
ignored. Completed human turns carrying an explicit author can still persist if
delivered asynchronously after a bot transition.

Bot or unidentified shared-session participation quarantines supplementary
session-end/checkpoint extraction, even after a human resumes. Checkpoints remain
durably archived under the neutral `__mixed__`/null-agent scope without extraction.
Quarantine is persisted in the private ledger and survives provider restart and
session rewind. Compression and branch continuations inherit the parent's scope,
author history, active denial, and durable quarantine. An explicit transcript reset
clears it once the current turn is authorized. Bot transitions invalidate cached
and in-flight recall generations.
Existing memories created before this protection are not automatically removed.

Switching back to a known session without a
`user_id` preserves that session's previously recorded author scope; the scope is
only overwritten on `reset=True` or an explicit `user_id`, and a brand-new session
without a parent still falls back to the default scope. Migration preserves source
scopes, including null agent identity, and tools cannot supply a scope override. Recalled text is
untrusted data and never becomes executable plugin instructions.
