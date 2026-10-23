# One Memory, Multiple Agents — opt-in shared owner

Before production deployment, read the [protected destination and doctor guide](deployment-security.md)
and [threat model](threat-model-v1.md). Runtime 0.5.0 includes opt-in destination protection. Qdrant write
credentials must belong exclusively to the Owner among application clients.

Several independent agents can use one canonical store through a single Runtime
owner. This checkout implements a local authenticated service and stdio MCP bridge.
Existing Hermes Native Provider, standalone stdio MCP, DSH and WorkBuddy defaults
remain private and unchanged. No installed profiles are reconfigured by this work.

See the [architecture/security contract](architecture.md),
[中文操作说明](README.zh-CN.md), and [observed validation report](report.md).
Do not use commands below against an existing production profile for experiments.

## Install and create a separate owner profile

Use Python 3.11+ and an isolated environment. Runtime 0.5.0 includes this
feature. Install the PyPI release, or use a reviewed checkout at an immutable
commit for development.

Check [PyPI](https://pypi.org/project/hermes-plugin-qdrant-memory/0.5.0/)
for Runtime 0.5.0 availability before package installation.

```sh
python3 -m venv /absolute/path/shared-runtime-env
/absolute/path/shared-runtime-env/bin/pip install 'hermes-plugin-qdrant-memory[mcp]==0.5.0'
/absolute/path/shared-runtime-env/bin/qdrant-memory setup \
  --profile /absolute/path/shared-owner --embedding-provider ollama \
  --model qwen3-embedding:0.6b --dimensions 1024 \
  --base-url http://127.0.0.1:11434 --user-id owner-admin
```

Install the selected Ollama model explicitly before setup. Setup creates a new
private embedded profile and validates its collection. A remote Qdrant Server or
Cloud can instead be configured in the owner profile using the existing runtime
configuration and `init` workflow. Keep those credentials only in the owner.
Never point multiple independent profiles at the same destination for writing.
All client embeddings run on the owner. Fingerprints, distance and dimensions
must match the existing collection. Changing a model requires an explicitly
created destination and the existing authorized migration procedure.

Generate three independent high-entropy tokens in private files and a policy,
without printing credentials. Run in a new operator-owned directory:

```python
import hashlib, json, os, secrets
from pathlib import Path

os.umask(0o077)
clients = []
for principal, permission in [("hermes", "write"), ("dsh", "write"), ("reviewer", "read")]:
    token = secrets.token_urlsafe(32)
    path = Path(principal + ".token")
    with path.open("x") as stream:
        stream.write(token)
    clients.append(
        {
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "principal": principal,
            "owner_id": "owner-admin",
            "space_id": "my-project",
            "permission": permission,
            "visibility": "shared",
        }
    )
with Path("policy.json").open("x") as stream:
    json.dump({"clients": clients}, stream, indent=2)
```

Keep the files out of Git. Policy must have POSIX mode 0600 (use private Windows
ACLs on Windows). `visibility: project` is another explicitly granted space. `visibility: private`
must have one principal per owner/space. A credential grants exactly one
configured space. Use a different credential/process for other spaces.

Start the Owner from the private directory that contains the policy.
The resolved `--policy` path must stay within the current working directory.

```sh
cd /absolute/path
/absolute/path/shared-runtime-env/bin/qdrant-memory \
  --home /absolute/path/shared-owner shared-serve \
  --policy /absolute/path/policy.json --port 8765
```

Wait for the readiness JSON. The endpoint is loopback-only. It is a private API,
**not** an HTTP MCP endpoint. Do not put it behind a public reverse proxy.
Audit allow/deny decisions go to stderr with hashed principal IDs. Capture and
retain these logs according to your local privacy policy. Stop with Ctrl-C. Service closure drains active requests before releasing
WriterLease.

## Private, shared, read-only and project spaces

A credential binds `principal`, `owner_id`, `space_id`, `permission` and
`visibility` in trusted configuration. `permission: read` rejects every write at
the service boundary. `write` permits reads and authorized changes. A
`visibility: private` space allows one principal only. `shared` or `project`
spaces can explicitly grant several agents. One token connects to one space, so
use a different credential and connection for another space.

The owner maps to the existing `user_id`. A stable space maps to the reserved
`agent_id: shared:v1:<space>`. The creator is stored in `created_by` and the
last updater in `updated_by`. Search validates scope, shared marker and
visibility together. ID mutations validate the same boundary. Records without
the explicit shared marker stay private, and the previous `agent_id: null` is
not a cross-agent wildcard. Tool arguments cannot override the owner, agent
identity or space. Sharing never imports private conversation history.

## Connect DSH, WorkBuddy, Codex or Hermes's MCP fallback

Each host launches its own bridge from the preview environment:

```json
{
  "mcpServers": {
    "qdrant-memory-shared": {
      "command": "/absolute/path/shared-runtime-env/bin/qdrant-memory",
      "args": ["shared-mcp", "--url", "http://127.0.0.1:8765"],
      "env": {"QDRANT_MEMORY_SHARED_TOKEN": "<inject this host's own token securely>"}
    }
  }
}
```

The placeholder is not a usable credential. Use your host's supported secret
injection mechanism. Never paste real tokens into source-controlled examples.
The stdio tool names remain `qdrant_memory_search/add/update/delete`. Shared
mode adds a required `request_id` for mutations. Update and delete also require
`expected_version`. Discover the actual catalogue.
For Codex's TOML MCP configuration use:

```toml
[mcp_servers.qdrant-memory-shared]
command = "/absolute/path/shared-runtime-env/bin/qdrant-memory"
args = ["shared-mcp", "--url", "http://127.0.0.1:8765"]
env_vars = ["QDRANT_MEMORY_SHARED_TOKEN"]
```

This is an explicit external MCP registration, not a change to a managed bundle.
DSH bundle and WorkBuddy managed installers still launch their private profiles.
The repository's companion MCP distribution contains separate preview config
examples. Host UI/native bundle import acceptance remains a follow-up.

**Hermes compatibility:** the Native Provider stays private by default and performs its
existing automatic extraction/prefetch. Runtime 0.5.0 supports opt-in Native Owner
shared extraction. See the [remote Owner guide](remote-owner.md). Use the shared MCP bridge only for a Hermes profile without the
qdrant-memory Native Provider interface enabled. Do not register both interfaces
for one Hermes profile. Alternatively, keep Native Provider and use the explicit
operator CLI for a user-authorized shared save. No lifecycle callbacks are faked.

## Walkthrough: explicit Hermes → independent DSH recall

Available at the runtime/stdio interface level. Actual host application UI
flows are not certified by the SDK tests. For a Hermes MCP-fallback profile:

1. The user authorizes saving a project decision into `my-project`. Hermes uses
   its own credential and `qdrant_memory_add` with
   `{"text":"Project uses SQLite WAL","request_id":"decision-save-001"}`.
2. A new DSH session uses its own credential and searches with
   `{"query":"Which database mode does this project use?"}`. The service returns
   the canonical record with `created_by: hermes`, ID and opaque `version`.
3. For an authorized replacement, DSH supplies that exact ID/version:
   `{"id":"<id>","text":"Project uses PostgreSQL","expected_version":"<version>","request_id":"decision-edit-001"}`.
4. Hermes searches again and sees the new text/version. An edit based on the old
   version returns `SharedConflictError`. It never overwrites the newer edit.
5. A reviewer credential can search but every add/update/delete is rejected.
   A private record, another user's record or a legacy record remains invisible.

A direct CLI call uses the same policy boundary:

```sh
# Export this client's token through a trusted local secret mechanism first.
qdrant-memory shared-call --url http://127.0.0.1:8765 \
  --tool qdrant_memory_search --arguments '{"query":"database mode"}'
```

Do not auto-share entire conversations. Retrieved text is untrusted data and
never authorizes a save, an edit or a grant change.

## Retry, backup, recovery, upgrade and rollback

Keep a stable unique request ID per intended mutation. A network failure is
ambiguous: retry the **same** request ID and exact arguments. The durable receipt
returns the original result. Reusing a request ID with other arguments conflicts.
A successful receipt is historical. Search for current state after reconnect.
For stale-version conflict, re-read and obtain intent for the new edit. Use a
new request ID only for that newly authorized operation. Never guess a new version.

The owner replays pending Runtime operations and unfinished request plans before
readiness. On terminal failure it blocks new writes and requires intervention.
Stop the owner. Restore the Qdrant/embedding dependency. Run the existing
operator `qdrant-memory --home OWNER retry`. Then restart. No second owner should run
maintenance concurrently. While shared plans are unfinished, shared write calls
and mutating init/delete/import commands fail closed. Shared search remains
available. Operator retry
remains available, followed by owner restart to reconcile receipts. Pending destructive intents and raw host extraction
events continue to block incompatible startup. Use a separate owner home rather
than reusing a Native Provider's active profile.

Stop the owner and take a coherent backup of **the whole profile including
SQLite WAL/sidecars, Qdrant embedded data and configuration**. For remote Qdrant,
coordinate a server snapshot with the stopped owner's profile backup. Preserve
policy and client tokens separately with private permissions. Restore matching
ledger/store snapshots together. Do not restore only one side of a completed
operation. Never copy an open embedded directory as a consistency guarantee. Portable
Mem0-format export remains an interchange operation, not a complete shared backup:
it does not preserve sharing grants, revision tokens or shared request receipts.
Re-imported records remain private until an explicit supported sharing action.

The feature adds a request table and optional payload fields. Existing
collections are validated, never silently migrated. Old code ignores the additive table and
fields. To roll back, stop the service, retain a coherent backup, remove the new
MCP registrations and return agents to their prior private profiles. Do not use
old standalone tools to mutate service-managed shared scopes. A legacy owner
cannot reconcile unfinished shared request plans. Keep the new runtime available
to drain and recover them before rollback. Downgrade is **not** safe recovery for a
pending shared plan.

## Limits and troubleshooting

- `WriterBusyError`: use the canonical service, or stop the old owner cleanly.
  Never remove locks. Separate profile homes do not add safe multi-writer support.
- `PermissionError`: check the host token/grant and ID space. Do not forge
  identity.
- `SharedConflictError`: stale revision or reused request ID. Reconcile
  explicitly.
- `SharedRecoveryRequiredError`: stop admission, repair dependency, retry/restart.
- Embedding fingerprint/dimension mismatch: keep original pipeline or perform an
  explicit backed-up migration to a new collection. Never change fingerprints.
- HTTP 403: exact loopback Host, JSON content type, no Origin, valid bearer token.
- Sharing does not imply increased quality. All searches and writes serialize
  through the owner. Local inference can be the throughput bottleneck. See the
  reproducible measurements, including local-hop overhead, in the report.
- Receipts are retained indefinitely in the MVP for retry correctness. Plan
  payloads are scrubbed after commit. No receipt compaction policy is released.
- No distributed multi-writer, direct embedded readers, OAuth, group
  administration UI or automatic private-to-shared migration is implemented.
  For HTTPS MCP and Native Owner extraction, use the separate
  [remote Owner guide](remote-owner.md).

The [official Qdrant MCP](https://github.com/qdrant/mcp-server-qdrant) offers
semantic storage/retrieval with Qdrant persistence and metadata filtering.
Qdrant Memory focuses on agent memory lifecycle, durable recovery and explicit
controlled sharing. This feature is part of Runtime 0.4.0. It remains opt-in
and operator-configured.

## Local HTTP security boundary

The shared owner uses an unencrypted private local HTTP API bound to `127.0.0.1`
only. Bearer credentials constrain configured clients. Malicious code running as
the same OS user, or a privileged local process, is outside the credential
confidentiality boundary. Do not expose this private API through a public proxy.
Remote clients must use the separate HTTPS MCP endpoint from Runtime 0.5.0.

The repository records one Sonar rule exception: `python:S5332` (cleartext HTTP)
applies the local transport design decision to `qdrant_memory/shared_service.py`
only. All other security rules and quality thresholds stay active. See the
[architecture note](architecture.md#reviewed-local-http-transport-decision-sonar-s5332)
for the rationale and tests. Remove this exception and review TLS and remote
identity authorization before adding a remote or wildcard listener.
