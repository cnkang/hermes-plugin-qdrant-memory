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
author takes precedence for their turn; bot turns and non-primary agent contexts
do not automatically write memories. Switching back to a known session without a
`user_id` preserves that session's previously recorded author scope; the scope is
only overwritten on `reset=True` or an explicit `user_id`, and a brand-new session
still falls back to the default scope. Migration preserves source scopes, including
null agent identity, and tools cannot supply a scope override. Recalled text is
untrusted data and never becomes executable plugin instructions.
