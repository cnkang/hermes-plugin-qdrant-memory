# HTTPS MCP Owner (unreleased source)

This capability is newer than Runtime 0.4.0. Do not infer its availability from
the unchanged source package version. Check `qdrant-memory --help` for
`remote-serve`, `remote-mcp`, `remote-event` and `shared-device`. No package has
been published by this work. Install a reviewed source checkout explicitly with
`python -m pip install '.[remote]'` in a private environment.

## Roles and topology

One active Owner holds one authoritative Ledger and one Runtime. Clients have
independent provisioned grants, never Qdrant keys. Storage remains independently
selectable: Embedded files on the Owner only, an HTTPS/API-key protected
self-hosted Qdrant server, or optional Qdrant Cloud. Cloud is storage, not the
Owner or the MCP transport. No SSH, VPN, account service or Docker is required
for normal deployment. Docker is used for isolated acceptance only.

The existing `shared-serve` private API stays literal loopback. The new listener
also binds literal loopback. Only a same-host TLS proxy is reachable remotely.
Native Hermes may itself host the Owner using its existing Runtime and host LLM.
Alternatively, a dedicated standalone Owner can serve all explicit clients and
the opt-in Pi lifecycle adapter with an explicitly configured extraction LLM.
Never run those two Owner roles against the same destination concurrently.
There is no multi-Owner fencing, automatic failover or HA support.

## Authentication and device policy

This iteration implements **pre-provisioned service credentials**, not OAuth.
OAuth 2.1/OIDC discovery, protected resource metadata, interactive authorization,
PKCE and general OAuth MCP Host interoperability are unsupported. Do not publish
an open or unauthenticated endpoint. Browser Origin requests are rejected.

Generate separate high-entropy credentials through a secret manager. A grant
contains the existing six string fields plus `logical_agent_id` and `device_id`.
Optional `expires_at` is a Unix timestamp. `revoked` is boolean. Only the SHA-256
token hash belongs in the policy. Example (hash is a placeholder):

```json
{
  "clients": [{
    "token_sha256": "<64 lowercase hexadecimal characters>",
    "principal": "hermes-device-a",
    "owner_id": "alice",
    "logical_agent_id": "hermes-personal",
    "device_id": "device-a",
    "space_id": "hermes-private",
    "visibility": "private",
    "permission": "write"
  }],
  "destination": "<existing protected destination binding object>"
}
```

The destination field above must be the real binding object described in
[deployment security](deployment-security.md). A string is deliberately invalid.
The administrative `shared-device validate` command rejects placeholders.
Use a second independent credential/principal/device with the same owner,
logical agent and private space for B2. A different logical agent must use a
different private space. Project grants may share one project space with
different logical agents and permissions. Tool arguments cannot supply identity.

Six-field legacy policies retain their original partition and private uniqueness
rules. New logical private grants use a separate `shared:v2:` partition with
logical identity included in its key. They do not expose old Native private or
legacy-null records. There is no automatic import or migration. Changing a
logical identity cannot publish that identity's prior private records.

From the private policy directory (POSIX mode 0700. Policy mode 0600):

```sh
qdrant-memory shared-device validate --policy ./owner-policy.json
qdrant-memory shared-device revoke --policy ./owner-policy.json --owner-id alice --device-id device-a
qdrant-memory shared-device rotate --policy ./owner-policy.json --owner-id alice --device-id device-b --output ./new-device.token
```

Revocation is repeatable and affects every grant of the selected device. Rotation currently requires exactly one active grant of that device. For a multi-space device, provision replacement grants explicitly and revoke the old device. Do not reuse one credential for different spaces. The token output must not exist. Rotation writes the new private token file before atomically replacing the policy. A crash can leave an unused token file, never an active token with no recoverable copy. Retrying to an existing output refuses overwrite. Admin commands serialize cooperating updates and fsync the replacement. Windows protected administration remains unsupported and fails closed.

Every request reloads and validates policy. A missing, malformed or unsafe policy
denies admission. Revocation/expiry applies at authentication/admission. Work
already durably accepted finishes its recorded plan. A policy replacement cannot
erase a previously delivered model context. The Host must clear or rebuild that
context. New recall always reauthenticates. Pi never persists shared recall in
session history. Native shared recall discards late reads, allows one in-flight
read, and defaults to a 50 ms delivery wait. Slow shared results are omitted.
For a slower local embedder, explicit `shared_read.delivery_timeout_seconds`
may increase that wait up to 0.25 seconds. The measured qwen3-embedding:4b shared
search p95 was about 94 ms on this test machine, so a 0.15–0.20 second budget may
be needed. This is a latency/freshness tradeoff, not permission caching.

## Standalone deployment

Use a dedicated unprivileged OS account, private directories, a service manager
with `UMask=0077`, and the existing protected destination policy. Initialize a new
test profile explicitly. Do not point it at a production collection or migrate
existing data automatically. Existing destination claims, embedding fingerprints,
schema checks, limits and recovery gates remain mandatory.

```sh
cd /srv/qdrant-owner
umask 077
qdrant-memory --home /srv/qdrant-owner/profile remote-serve \
  --policy ./owner-policy.json --local-port 8765 --port 8766 \
  --allowed-host memory.example.com
```

The CLI's `bound` announcement means sockets were bound, not a successful TLS or
authenticated MCP health check. Probe the externally configured `/mcp` using a
read-only credential before treating deployment as ready. An unauthenticated
probe gets 401/403 and no storage details. Both listeners drain before releasing
the lease. Do not configure a short supervisor kill timeout that abandons writes.

An Nginx TLS-only example (replace the certificate paths, account and hostname):

```nginx
limit_req_zone $binary_remote_addr zone=memory_pre_auth:10m rate=10r/s;
limit_conn_zone $binary_remote_addr zone=memory_conn:10m;
server {
    listen 443 ssl;
    server_name memory.example.com;
    if ($host != memory.example.com) { return 444; }
    ssl_certificate /etc/ssl/memory/fullchain.pem;
    ssl_certificate_key /etc/ssl/memory/privkey.pem;
    access_log off;
    client_max_body_size 128k;
    client_body_timeout 10s;
    client_header_timeout 10s;
    location ~ ^/(mcp|events|events/manage)$ {
        limit_req zone=memory_pre_auth burst=20 nodelay;
        limit_conn memory_conn 8;
        if ($http_forwarded != '') { return 403; }
        if ($http_x_forwarded_host != '') { return 403; }
        proxy_set_header Host memory.example.com;
        proxy_set_header Forwarded '';
        proxy_set_header X-Forwarded-For '';
        proxy_set_header X-Forwarded-Host '';
        proxy_set_header X-Forwarded-Proto '';
        proxy_pass http://127.0.0.1:8766;
        proxy_buffering off;
        proxy_read_timeout 120s;
    }
}
```

Do not enable Uvicorn forwarded-header trust. Pin Host authorities exactly. Wildcards are rejected. Origin is rejected, and forwarded metadata is never an
identity source. The Gateway bounds body size to 128 KiB. After authentication,
it admits 32 concurrent requests and four per principal. It sets body-read
inactivity to 10 seconds. It allows 240 requests per minute per authenticated
principal. Authentication work has a separate worker limit. Unauthenticated
requests cannot consume authenticated
body slots or quotas. Per-source limits and TLS/header timeouts at the proxy are
required. The example sets header and body timeouts; the TLS handshake timeout
belongs to the terminating proxy. Application quotas do not prevent
network-layer denial of service: the
listener accepts connections before Host, bearer and quota checks run, so the proxy
must cap concurrent connections per source (`limit_conn`) and keep the listener on
loopback. The listener closes connections whose request headers are incomplete
after 10 seconds, including requests on reused connections. Complete headers end
that deadline; active MCP requests use the gateway's existing deadlines.
Set an OS descriptor limit (`LimitNOFILE=4096` in systemd) as a further resource
bound. A local process that bypasses the proxy can still exhaust that limit
through a rapid connection flood. Logs must not retain
Authorization, query text, raw memory or exception bodies. HTTP client redirects
and ambient proxy settings are disabled. TLS validation cannot be disabled.

For a direct HTTP-capable MCP Host, configure `https://memory.example.com/mcp`
and its independent Bearer service credential using the Host's secure settings.
For a stdio-only Host:

```sh
qdrant-memory remote-mcp --url https://memory.example.com/mcp \
  --token-env QDRANT_MEMORY_REMOTE_TOKEN
```

The token is injected through the Host's secret environment, never command arguments.
`--ca-file` adds explicit trust for a private CA. It never disables verification.
This is MCP-to-MCP using the official SDK, not exposure of `/v1/tool`.

## Native Hermes Owner

Use one MemoryProvider. In the Native profile's configuration, explicitly set:

```json
{
  "native_owner": {
    "enabled": true,
    "policy": "owner-policy.json",
    "port": 8765,
    "private_token_env": "HERMES_PRIVATE_TOKEN",
    "shared_capture_token_envs": ["ALPHA_CAPTURE_TOKEN"]
  }
}
```

The private token must bind the configured human owner, one logical Hermes agent
and a writable private space. Project capture tokens must also carry a nonempty
administrator-authored `capture_rule_id`. The configuration list is a second
explicit opt-in. Without that list, extraction stays private. The host's normal
LLM routing is inherited. This role uses the existing Native worker, Ledger,
Runtime and WriterLease. New private data occupies the device-granted partition. Old private data is preserved and is not silently imported. Turning the role off
restores the original private partition. Keep the configuration and its policy
together for consistent restore/rollback.

Optional `remote_port` and `allowed_hosts` enable the same loopback MCP listener
under the Native Owner, and require a protected destination policy. A same-host
TLS proxy is still required. The Native lifecycle itself uses real Hermes callbacks.
Optional `enable_lifecycle: true` enables the authenticated `/events` extension
for other official Host adapters, reusing this Runtime and inherited host LLM.
It requires the protected remote listener and defaults off. Do not register a
second MCP memory interface in the same Native profile.

Native Owner private recall revalidates its current device grant before delivery.
In Native mode, the Owner grant is the only authority for the memory tools. A
session with a different scope gets a permission error. The code does not fall
back to the private local dispatch. Thus, no other identity can write into the
granted private partition outside that grant.
A Runtime mutation generation invalidates private cached recall before any write
attempt, including remote deletion and uncertain acknowledgements. Failed Native
storage operations gate Owner writes and automatic recall, retaining queued raw
events for explicit recovery. Legacy private cache behavior is unchanged when
Native Owner is disabled.

When a project capture rule is denied, private capture continues and a durable
denial counter records the skipped rule. Other devices' writes remain available.
When the private credential itself is denied, the raw turn is retained FAILED
without blocking unrelated clients. Restore authorization, stop the Owner,
run the existing explicit operator `retry`, then restart. Reauthorization of an
unadmitted turn recovers private data only. It never retroactively publishes
that turn into a project. An identity mismatch fails closed.

## Non-Hermes automatic extraction

For a standalone Owner, `--enable-lifecycle` is explicit and off by default. It
requires a configured LLM override, for example an Owner-local Ollama OpenAI
compatible endpoint. Real model compatibility remains a separate acceptance:

```json
{
  "llm": {
    "mode": "override",
    "provider": "openai-compatible",
    "model": "<explicit extraction model>",
    "base_url": "http://127.0.0.1:11434/v1",
    "api_key_env": "QDRANT_MEMORY_LLM_API_KEY"
  }
}
```

Extraction reuses `Extractor`, Runtime dedupe, operations and the Ledger. Raw
events are durable before extraction, response loss replays the event receipt,
and failed extraction is retained for operator retry. Default capture is private. Project/shared capture additionally requires the grant's audited capture rule.
The `/events` extension is separate from MCP and does not invent a fifth CRUD
tool. Current event contract is `turn_completed`. Other proposed lifecycle event
kinds are not supported yet. A 128-event pending/failed backlog gate applies.

## Compatibility, backup and rollback

Local evidence covers Python 3.14 + MCP SDK 1.30.0 and Python 3.11 + SDK 2.3.0.
The official manager handles transport negotiation, including legacy initialized
2025-11-25 requests. Real SDK 1.x and 2.x HTTPS clients are separately tested.
Exhaustive mixed-SDK/Host interoperability and OAuth Host modes are not claimed.
The code retains SDK 1.x decorator and SDK 2.x constructor APIs. Only the remote
bridge switches between SDK-required httpx and httpx2 client types.

Before upgrading, stop the single Owner and drain requests. Then take a
consistent backup. Include the Qdrant destination, the authoritative Ledger with
SQLite WAL state, the protected claim/registry and configuration/policy. Keep
credential secrets in the secret manager. Never copy an open Embedded directory
to another device or open it from a second process. For Server/Cloud, coordinate
a database snapshot with the quiescent Ledger. Restore and verify the entire
matching set on an isolated destination before switching clients. New device
grants and additive lifecycle tables cannot be interpreted by 0.4.0. Roll back
to the saved pre-upgrade snapshot/config, not by deleting claims, WAL, locks or
new tables.

macOS/Linux are the primary targets. Existing platform CI exercises portable
contracts on Windows. Protected public service/admin remains unsupported there.
Real Cloud, production TLS deployment, real model quality and user Host UI
acceptance require independent evidence. No production-readiness claim is made.

Native lifecycle turns, builtin memory notifications and mixed-history checkpoints
also require the Owner scope. A different or neutral mixed scope is recorded as a
failed `native_owner_unadmitted` event and raises `PermissionError`. It cannot
fall back to local Runtime writes. Operator retry cannot reattribute such an event to
another identity.

After operator retry, startup cancels pending `native_owner_unadmitted` input
whose scope differs from the current Owner. It never reattributes that input.
Only raw input without candidates, operation keys or Owner authority qualifies.
Its `CANCELLED` receipt, erased payload and `native_unadmitted_discarded` counter
commit atomically. Repeated startup does not count the same cancellation again.
Same-scope input still reauthorizes into private memory, and admitted/prepared
work retains its recovery protections. Failed events still require the existing
explicit operator retry. This does not bypass the failure gate.

An operator can also discard a specific unadmitted event before restart. Stop
every Owner for the profile before using the offline command:

```sh
qdrant-memory --home /path/to/owner-profile discard-unadmitted-event \
  --event-id EVENT_ID --confirm-discard
```

The standalone distribution installs this `qdrant-memory` entry point. `--home`
is a global option before the subcommand. For a host-only plugin installation,
select the intended Hermes profile first, then use
`hermes qdrant-memory discard-unadmitted-event --event-id EVENT_ID --confirm-discard`.

This requires the existing profile's exclusive writer lease and does not open
Qdrant, an embedding service or the LLM. It resolves no service credentials.
Only a `PENDING`/`FAILED` `native_owner_unadmitted` event without candidates,
operation keys or Owner authority can be discarded. The change uses a
compare-and-set on the original status and payload, retains a `CANCELLED` receipt,
and clears the raw payload. Repeating the same disposal is a no-op. Prepared or
uncertain work must reconcile instead. This command cannot clear its recovery
gate, reset a destination or delete saved memories. It is an operator CLI,
never an Agent MCP tool. Automatic startup cancellation is limited to the
foreign, never-admitted input described above.
