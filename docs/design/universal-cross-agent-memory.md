# Universal cross-agent and cross-device memory — Phase 0 design record

Status: design accepted. The implementation is phased (see Section 9).
Date: 2026-10-19. Owner: project maintainer. This document is for future contributors.

## 1. Baseline

The table below shows the commits that this design reviewed.

| Repository | Commit | Notes |
|---|---|---|
| hermes-plugin-qdrant-memory | `3f579f15389c497056a44636321be64c35b82bd7` | Runtime 0.4.0 is released. Protected destination, account-local claims, attributed receipts and shared-doctor are in main, but they are not in 0.4.0. |
| qdrant-memory-mcp | `60d0cda936dc248ba26a7a1c12ba84ea3643f6ec` | Distribution 0.2.2. |
| skills | `d6062f07f35a741d5058712ab5e9966b39f87e00` | Skill 0.3.1, snapshot skills-20261019.1. |
| hermes-agent (host) | `8f5693af41` | No change. |

Note: docs/shared-memory/architecture.md describes the 0.3.0 baseline. This text is
stale. A later documentation commit corrects it.

## 2. What exists today

| Capability | Status | Evidence |
|---|---|---|
| Private memory through the Hermes native provider | Implemented and tested | provider.py supplies initialize, on_turn_start, sync_turn, prefetch, on_pre_compress and shutdown. The provider acquires WriterLease.for_config at line 102. |
| Four explicit tools over stdio MCP | Implemented and tested | mcp_server.py uses stdio_server. standalone.py supplies the mcp command. |
| Local shared owner with a loopback JSON API | Implemented and tested | shared_service.py binds the literal address 127.0.0.1. It checks the Host header and the bearer grant. |
| stdio bridge to the owner | Implemented and tested | shared_client.py supplies call and serve_mcp. |
| Grants | Implemented | A grant has six fields: token_sha256, principal, owner_id, space_id, permission and visibility. |
| Receipts and attribution | Implemented | shared_requests_v1.principal, created_by and updated_by. |
| Remote HTTPS MCP | Absent | No ASGI or HTTP MCP transport exists. |
| logical_agent_id and device_id | Absent | The grant schema does not contain them. |
| Automatic memory for hosts that are not Hermes | Absent | These hosts use the explicit tools or the skill. |
| OAuth or OIDC | Absent | Bearer grants only. |

Correction: one principal can have more than one credential. _validate_grant requires
unique token_sha256 values only. Thus, two grants can share one principal. Today, B2 uses
grants with the same principal. This method gives no per-device attribution and no
per-device revocation. For this reason, the design needs device_id and logical_agent_id.

## 3. Process model

The provider acquires a write lease in initialize and opens the profile store. The shared
owner does the same operation for its own profile. Two processes must not open one
destination at the same time. The lease is local and cooperative. It cannot stop a
process on a different host, on a different OS account, in an older binary, or a direct
database writer.

For this reason:

- The owner is the only process that opens storage, calculates embeddings, or holds the
  database write credentials.
- A Hermes profile that needs shared memory operates as a client. The client has no
  store, no lease and no database credentials. The client sends requests to the owner
  through the loopback client with a grant.
- A profile cannot have both roles for one destination. The configuration stops with an
  error in this case.

Thus, Phase 1 is a client-side reader.

## 4. Target topology

```text
Device A: Hermes (client) -- stdio or local bridge --> Owner (one process) --+
Device A: Codex or DSH (stdio) -- local bridge ------> Owner                 |
Device B: remote client ---- HTTPS /mcp -------------> same Owner            |--> Qdrant
```

The remote listener is a second transport in the owner process. Both transports get the
grant from the credential, and then call the same SharedOwner.call function. There is one
ledger, one CAS path and one admission check. The shutdown sequence drains both listeners
before the owner releases the lease. The owner reports readiness only after both
listeners are bound.

The transport name is Streamable HTTP.

## 5. Transport options

| Option | Shape | Decision |
|---|---|---|
| A. Second listener in the same process | The owner starts an ASGI application with the official Streamable HTTP session manager. The listener binds 127.0.0.1 or a Unix socket. A reverse proxy on the same host terminates TLS. | Recommended |
| B. Separate gateway process | A new service holds no storage. It sends authenticated tool calls over the loopback API. | Acceptable fallback. It adds a second hop and a second token path. |
| C. Replace the loopback server with ASGI | One application supplies the JSON API and MCP. | Rejected. This option rewrites a tested security boundary and gives no product gain. |

A boolean flag for "TLS proxy acknowledgement" is not a control. Thus, the listener binds
a loopback address or a Unix socket only. A bind to a different interface causes an error
at startup. The deployment guide puts Caddy or Nginx on the same host. The proxy
terminates TLS. This rule also keeps the security exception for the old loopback HTTP
code in one file, shared_service.py.

For a container deployment, the proxy container and the owner container must share a
network namespace. As an alternative, the proxy container uses a shared mount for the
Unix socket of the owner. The two-container test uses one of these methods and shows that
the proxy can connect to the owner.

The remote listener must not use the private create_server(runtime, ...) factory. A
separate create_shared_server(owner, grant_resolver) function supplies the shared schemas
only. It has no runtime parameter. Thus, a wiring error cannot expose the private tools
of the owner.

## 6. Identity model

The policy load must obey these rules:

1. One (owner_id, space_id) pair has one visibility value.
2. A private space has one logical_agent_id. More than one credential can map to this
   identity. Each credential has its own principal and device_id. No other logical agent
   can access the space.
3. A shared or project space can have more than one logical_agent_id. Each one has its
   own permission.
4. One device_id belongs to one credential. The administrator can revoke one device_id
   alone.
5. Tool arguments do not contain these fields. The owner gets them from the credential.

B2 (one logical agent on two devices with private memory) needs a private partition
mapping. It must not use a wider shared grant. A shared grant resolves to the reserved
scope shared:v1:<space>. This scope is different from the private scope (user_id,
agent_id) of the native provider. Thus, a device credential for the same logical agent
must map to the same private partition. One method binds (owner_id, logical_agent_id) to
the existing private agent_id. A shared or project grant must not supply this access,
because it increases the visible data. Phase 3 designs this mapping. The mapping is
necessary before public exposure.

Attribution stays at the principal level. Thus, receipts and created_by/updated_by show
the device that did the operation. logical_agent_id is a group key, not an attribution
value.

## 7. Threat model delta

| Threat | Control |
|---|---|
| Accidental public bind | The listener accepts a loopback address or a Unix socket only. A bind to a different interface causes an error at startup. |
| Proxy header spoofing | The configuration pins the trusted proxy addresses. The software validates the Forwarded, Host and Origin headers. It removes forwarded headers from client requests. |
| Token replay | Each device has its own credential. Receipts deduplicate writes by principal and request ID. This method gives idempotency, but not replay protection. The software checks expiry and revocation for each request. |
| Revocation latency | The policy reloads on SIGHUP or on an mtime change. A revoked token fails on the next request. A restart is not necessary. |
| Same-agent impersonation | The policy supplies logical_agent_id. The request cannot supply it. |
| Cross-space leakage | Each query contains the owner, the space and the visibility. A shared record must have the shared_protocol marker. |
| Reserved namespace abuse | The configuration load rejects the prefix shared:v1: in a private scope.agent_id. |
| Resource exhaustion | Limits apply to authentication attempts, body size, concurrency and request time. The shutdown sequence drains the connections. |
| Ledger divergence | One owner is active. A client-mode profile refuses write credentials. No high-availability claim exists. |

## 8. Deployment topology

- Public VPS: the owner and the proxy run on the same host. Qdrant runs in embedded mode,
  on a private interface, or in Qdrant Cloud.
- LAN: the owner runs on a small host. Clients use the LAN address through the same TLS
  proxy.
- Device behind NAT: the device cannot receive connections. The supported solution is an
  owner that clients can reach. A tunnel is an optional third-party component. It is not a
  product requirement.

## 9. Phases and the first pull request

- Phase 0: this design record and its review notes.
- Phase 1a (first pull request): an additive SharedReader library and the configuration
  schema for space-to-credential bindings. No provider wiring. No visibility change. The
  reader uses an explicit allowlist and revalidates the provenance. Tests only.
- Phase 1b: connect the reader to prefetch and search behind an explicit flag. This step
  changes the visible data for enabled profiles. The flag is separate, and the review
  covers this step. After a revocation, the host must clear or rebuild the related
  context before the next model call, or the smaller guarantee applies (see the review
  notes).
- Phase 2a: the Streamable HTTP listener that binds a loopback address or a Unix socket,
  the shared server factory, and the remote_design doctor check. No public exposure. A
  two-container HTTPS test runs against the same-host proxy.
- Phase 2b: public exposure. This phase starts only after Phase 3 supplies the credential
  lifecycle and after the ten tests in Section 10 pass. Identity and revocation come
  before exposure.
- Phase 3: the identity fields, the private partition mapping for B2, the management CLI,
  revocation without a restart, and migration.
- Phase 4: lifecycle adapters for hosts with public hooks.

## 10. Tests before the first remote listener

1. Default off: no remote socket exists. The loopback behavior is byte-identical.
2. Bind: the listener uses a loopback address or a Unix socket only. A wildcard bind
   causes an error.
3. TLS: a plaintext connection to the public port fails. The TLS client rejects an
   expired server certificate. If the operator enables mutual TLS, the proxy (the TLS
   server) rejects an expired client certificate.
4. Proxy trust: the server rejects untrusted Forwarded, Host and Origin headers. It
   removes spoofed client headers.
5. Authentication: a missing, malformed, expired or revoked token gives the response 401
   or 403. No token appears in the logs.
6. Shared surface: tools/list contains the shared schemas only. A private record is not
   reachable.
7. Isolation: a token for space X cannot read space Y. Legacy records without the marker
   stay invisible.
8. Recovery: a crash during a write gives the same receipt state as the loopback path.
9. Non-regression: the existing stdio MCP, scope, ledger and reset tests do not change.
10. End-to-end: a client container connects to the proxy of the owner container through
    real TLS. The proxy reaches the owner through a shared network namespace or a shared
    socket mount. The owner uses embedded Qdrant. No cloud account is necessary.

## 11. Design review

An independent design review was completed on 2026-10-19. Each finding that the code
could reproduce was accepted and added to this revision. The file
[review-notes-2026-10-19.md](review-notes-2026-10-19.md) contains the themes and their
resolutions. High-risk implementation work also needs a review of the real patch.
Phase 2b stays blocked until the identity work exists.

## 12. Non-goals

This design does not include multi-owner high availability, distributed locking, a
management UI, a memory graph, automatic migration of legacy private records, or SSH and
VPN as a requirement.
