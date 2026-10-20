# ADR: One Memory, Multiple Agents (released in Runtime 0.4.0)

## Addendum: protected destination admission (unpublished)

The optional destination binding and account-local claim extend cooperating
Runtime lease admission across profile homes. They preserve the destination
namespace, shared protocol, Qdrant payloads and Ledger schema. See the
[threat model](threat-model-v1.md) and [deployment contract](deployment-security.md).
The claim is trusted local configuration, never a payload ownership marker or
distributed fence. An Owner's exclusive account lock cannot enroll while an
updated private writer holds its shared account lock. Persistent claims then
reject ordinary writer admission until an explicit offline operator transition.
The existing profile lease remains held through worker/request drain.

Decision date: 2026-10-17. Status: implemented opt-in MVP, release gates recorded
in the [validation report](report.md). Hermes compatibility is the first acceptance gate.

## Baseline

All three repositories began clean on `main`, matching upstream HEAD checked
with `git ls-remote origin HEAD` on the decision date. No existing user files
were changed.

| Repository | Initial HEAD | Package version | Local describe |
|---|---|---|---|
| hermes-plugin-qdrant-memory | c24ac4f0fd0d67be5aadfc3d6719f31339820089 | 0.3.0 | v0.3.0-4-gc24ac4f |
| qdrant-memory-mcp | 36ff391bf835eb4adeeac9f0bd759a97403de59a | 0.1.4 | dsh-bundle-v0.1.0 |
| skills | c2d755cd6df3f9af069d9faff10fbcfa5b29806e | skill 0.2.6 | skills-20261016.2-2-gc2d755c |

## Existing guarantees and limits

`ownership.WriterLease.for_config` canonicalizes backend destination/collection,
but stores its OS lock below the profile home. Acquisition is nonblocking.
ExitStack releases it after store/ledger/embedder cleanup. It excludes a second
owner of the same profile destination, including read sessions using
`standalone.open_runtime`. It is **not a distributed lock**, nor an exclusion
mechanism across separate profile homes. Embedded Qdrant also owns its directory
lock. Independent processes must not open that path, even as nominal readers.

`Ledger` stores SQLite WAL with synchronous FULL. Rows are destination-namespaced
within the profile database (`config.ledger_namespace`), not a global coordinator.
Operation keys hash destination, point, action, full prepared payload and source
identity/version. `models.point_id` uses UUIDv5 over exact user/agent/source/source
identity. These algorithms remain unchanged. Runtime's RLock serializes
prepare/dedupe/commit, and remote mutations use `wait=True`. Pending operations
replay after restart. Terminal failures require authorized CLI retry. Delete
fences prevent older prepared work from resurrecting deleted facts. A collection
fingerprint point validates the embedding pipeline. Dimensions and distance
are validated before admission. No client-supplied vectors are accepted.

`tools.dispatch` authorizes exact IDs using `store.get(id, scope)` before
writes. Search always applies exact `user_id` and `agent_id` filters. Null agent is an
exact null partition, never a wildcard. Standalone argument validation rejects
identity overrides. Hermes Native Provider additionally derives scope from human
turn provenance, quarantines unattributed/mixed histories, persists extraction
events, and uses a context-bound worker. Its automatic extraction can perform
relation-based updates. Explicit add only deduplicates exact content. None of
these lifecycle paths are redirected into shared spaces.

DSH/WorkBuddy managed installers provision private profiles and pinned runtime
artifacts. Sharing is never automatically enabled on those installations.

## Alternatives

| Dimension | A: coordinated direct clients | B: single owner service | C: independent distributed writers |
|---|---|---|---|
| Correctness | Remote readers possible. Independent writers unsafe | Existing Runtime lock plus CAS revisions | Needs distributed CAS, fencing and coordination |
| Crash recovery | One designated writer ledger only | Existing ledger plus durable request plans | Multiple ledgers can replay conflicting old state |
| Security | Qdrant access must independently enforce scopes | Credential-to-fixed-space boundary | Distributed identities and policy consistency |
| Performance | Readers avoid owner hop | One extra local hop. All operations serialized | Potential parallelism, not established |
| Complexity | Safe reads need separate read-only store API | Small adapter around existing engine | High: reconciliation, split brain, tombstones |
| Operations | Must prevent other direct writers manually | One supervised process/profile and backup | Distributed coordinator and recovery tooling |
| Compatibility | Existing standalone reader acquires lease | Additive commands, existing defaults preserved | Risks changing identity/recovery semantics |
| Local deployment | Embedded independent opens unsupported | Embedded or Server, only owner opens backend | Embedded multi-process not supported |
| Remote deployment | Server readers technically feasible | Local agents can use owner with remote Qdrant | Requires authenticated remote protocol design |
| Maintainability | Two access paths to audit | One trusted policy boundary and engine | Large correctness surface |

Choose B. A's read-only remote access is feasible without a writable ledger,
but deferred: it would require a new credential/filter boundary and embedding
pipeline validation path. MVP readers use the owner and cannot write even if
they know tool names. C is rejected for this iteration. Qdrant write ordering is
not an atomic application-level read-check-write transaction across clients.

## Identity and data model

Each **operator-configured** credential hash grants one principal one fixed
`owner_id`/`space_id`, visibility and permission. A space maps to existing
`Scope(user_id=owner_id, agent_id="shared:v1:" + space_id)`. The caller agent is
`principal`. It is deliberately distinct from the stable storage partition.
Private grants require a unique principal for that owner/space. Shared/project
grants may authorize multiple principals. Use separate credentials for separate
spaces. Tool calls cannot select or widen a space.

Payloads retain schema-v1 core fields and add `shared_protocol`, `space_id`,
`visibility`, `created_by`, `updated_by`, and `version`. `created_by` survives
updates. `version` is an opaque random UUID revision, an equivalent CAS token
rather than a counter. Delete/recreate gets a fresh revision and avoids ABA.
Every shared search requires the protocol marker, visibility, space and exact
scope. Exact-ID mutations verify both scope and marker before version checking.
**All legacy records remain private**, including records already using null-agent
or the reserved scope string. No payload or collection migration is performed.

## Write protocol and recovery

1. Authenticate, validate a strict schema and the fixed grant, and reject
   read-only writes.
2. Hold the same Runtime RLock across scoped lookup and version validation.
3. Persist the complete immutable plan, the authenticated principal and the
   response in destination-namespaced `shared_requests_v1` within the same
   SQLite database (additive table only). Attribution comes from the token's
   grant, never from tool arguments.
4. Enqueue that exact plan through Runtime, commit using its retry/embedding/store
   path, and mark the receipt done **only after COMMITTED**.
5. Scrub completed plans. Retain request input hashes and responses for safe
   retry.

A crash before plan persistence admits no operation. A crash after plan persistence
but before operation enqueue recreates the operation from the plan. A crash after
Qdrant write but before acknowledgement replays the same deterministic point and
revision. A crash before receipt completion reuses the committed operation.
Recovery drains unfinished plans before service readiness. Receipt lookup is
credential-principal/space-bound. Using one request ID for other input conflicts
is rejected.
A disconnect never cancels admitted work. Completed receipts return the original
result even if the record was subsequently changed/deleted: they are operation
receipts, not current-state reads. Search to reconcile current state.

After an uncertain write error, new writes fail closed until restart/recovery.
Terminal FAILED operations require operator `retry`. Service startup refuses
readiness if a request cannot be reconciled. Concurrent stale update/delete
returns `SharedConflictError`. Re-read and obtain user intent before issuing
a new request. Do not transparently retry stale edits against a newer version.

There is no distributed fencing. Operators must use **one canonical owner home**
per destination and keep Qdrant credentials/data-directory access from agents.
A second differently configured profile or another host can bypass a local
lease. This deployment is unsupported. SQLite and Qdrant do not form one atomic database
transaction: deterministic replay closes the crash gap under this single-owner
assumption. External direct writes invalidate these guarantees.

## Transport/security

The service is an authenticated **private local JSON API**, not Streamable HTTP
MCP or an OAuth resource server. It binds literal IPv4 loopback only. It validates
Host/Origin, rejects browser origins, transfer encoding and oversized requests,
bounds idle sockets and worker count, and disables request logging. Clients
reject non-loopback URLs, environment proxies and redirects. Secrets are supplied
through environment variables. Policy stores only the SHA256 of high-entropy tokens
and requires owner-only POSIX permissions. Windows requires operator-managed ACLs.
Audit stderr records hashed principals and allow/deny only. It contains no
memory text, credentials or raw errors. Policy changes/revocation require restart. Protect/supervise the
service and rotate client credentials as local OS account security requires.
Same-OS-user malicious code is outside this credential boundary. It can
inspect process environments, configuration and storage. No public endpoint, CORS or multi-host access.

Agents use official SDK **stdio MCP** bridges. Only the opt-in shared catalogue
requires `request_id` and update/delete `expected_version`. Existing stdio and
Hermes schemas are unchanged. This is not a claim of host-native integration tests
for DSH/WorkBuddy/Hermes applications. Native Provider automatic sharing is deferred
until there is an explicit host author/space consent contract. The provider stays
private, and duplicate Hermes interfaces must not be registered.

## Upstream references checked

- [MCP 2025-11-25 transport specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports): stdio lifecycle and local HTTP security guidance.
- [Official Qdrant MCP](https://github.com/qdrant/mcp-server-qdrant), including
  `src/mcp_server_qdrant/qdrant.py` on master: semantic storage/search, metadata
  filtering, remote/local persistence. No claim that it lacks persistence,
  isolation or reliability. Qdrant Memory adds agent lifecycle/recovery management
  and this opt-in controlled sharing protocol.
- [Qdrant points documentation](https://qdrant.tech/documentation/concepts/points/)
  and [distributed deployment](https://qdrant.tech/documentation/concepts/distributed_deployment/).
  The distributed deployment documentation was not accessible at review time.
  No unverified distributed guarantees are inferred from it.

### Maintenance interlock

A plan may be durable before its Runtime operation exists. Therefore standalone
private tool sessions and legacy mutating maintenance (init/reset, scope delete,
import) inspect the optional request table **after acquiring ownership**. They
refuse unfinished shared work with `SharedRecoveryRequiredError`. This uses a
read-only SQLite connection and creates no schema/files in legacy profiles.
Operator `retry` remains available to repair terminal Runtime failures. The
owner then reconciles receipts. This prevents reset/delete followed by delayed plan
replay from resurrecting records. Read-only diagnostics/export remain available.
After owner reconciliation, existing deletion/reset operations work as before.
The Native Provider implementation and its lifecycle/schema contracts are unchanged.
The checkpoint acceptance test supports the latest host relocation of
`CompressionCheckpointUnavailable` while retaining the minimum-host import path.


## Reviewed local HTTP transport decision (Sonar S5332)

The optional shared service deliberately uses an unencrypted private HTTP API
bound to literal IPv4 loopback. This follows the local-only MVP transport decision.
TLS certificates and remote access are outside this iteration. Bearer credentials
separate configured clients within the documented local OS trust boundary. They
do not provide confidentiality against a privileged local process or hostile code
running as the same OS user.

`sonar-project.properties` records one rule/resource exception: `python:S5332`
for `qdrant_memory/shared_service.py`. This is the component that implements the
private HTTP API. The Sonar quality thresholds and all other security rules remain
active, including SSRF analysis of the client. The policy is recorded explicitly in the repository,
rather than hiding the finding with a source annotation.

The exception depends on the literal `127.0.0.1` bind, exact Host/port validation
and Origin rejection. It also depends on fixed authenticated grants, disabled
environment proxies and redirects, and the private OS account boundary. Tests
exercise those restrictions, verify DNS-independent loopback binding, and verify
that shutdown drains admitted requests before releasing ownership. Remove the
exception and review TLS and remote authorization before adding any
wildcard/remote listener or public proxy.
