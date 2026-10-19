# Protected Shared Owner deployment

This checkout adds opt-in destination protection and `shared-doctor`. Check the
installed command's help before using them; published Runtime 0.4.0 does not
provide these additions. The release candidate remains unpublished.

The Owner coordinates authorized tools through its Ledger and revision checks.
**Direct Qdrant writes bypass both. No local policy can prove that another host
has no administrative database key.** See the [threat model](threat-model-v1.md).

## Policy and enrollment

Keep the existing `clients` grants and optionally add a `destination` object:

```json
{
  "backend": "embedded",
  "destination": ["embedded", null, null, null, "/srv/memory-owner/qdrant-memory/qdrant"],
  "collection": "qdrant_memory",
  "embedding_fingerprint": "<SHA256 of the configured embedding pipeline>",
  "owner_profile": "<digest of the canonical absolute Owner profile path>",
  "schema_version": 1,
  "security_mode": "local-embedded"
}
```

This is the value of `destination`, not a complete grants policy. Prepare it from
the trusted Owner configuration using the same public Runtime helpers:

```python
from pathlib import Path
from qdrant_memory.config import backend_destination, embedding_config, fingerprint
from qdrant_memory.models import digest
from qdrant_memory.standalone import load_standalone_config

home = Path("/srv/memory-owner").resolve()
cfg = load_standalone_config(home)
binding = {
    "backend": cfg["qdrant"]["mode"],
    "destination": list(backend_destination(cfg["qdrant"])),
    "collection": cfg["qdrant"]["collection"],
    "embedding_fingerprint": fingerprint(embedding_config(cfg)),
    "owner_profile": digest(str(home)),
    "schema_version": 1,
    "security_mode": "local-embedded",
}
```

Review the binding, add it to the private policy and keep it out of source control.
The remote canonical tuple is `['remote', scheme, hostname, effective_port,
path_without_trailing_slash]`; Server and Cloud retain their existing common
namespace semantics. Credentials never participate in destination identity.

Security modes are `local-embedded`, `protected-server`, `protected-cloud` and
`external-unverified`. Protected remote modes require HTTPS and a credential, but
these checks do **not** certify its permission scope. External unverified mode
is explicitly weaker and unsuitable for claims of database write isolation.

Before enrollment, stop every writer, including old Runtime versions. Initialize
the collection with existing setup/init procedures and inspect it; never reset it
to resolve a protection error. Set the Owner home and data directories to mode
0700, files to 0600, with the Owner UID. Reject symlinks and hardlinked state files.
The protected service uses umask 0077 for new state. It checks privacy before
recovery and again before admitting writes. Unsafe existing modes are rejected,
not silently repaired. Private defaults are otherwise retained.

```sh
cd /srv/memory-owner
qdrant-memory --home /srv/memory-owner shared-doctor --policy /srv/memory-owner/policy.json
qdrant-memory --home /srv/memory-owner shared-serve --policy /srv/memory-owner/policy.json
```

For CLI invocation, the policy must resolve inside the current working directory,
just like explicit maintenance input/output files. Start from the private Owner
directory or another deliberately chosen enclosing directory. Paths that resolve
outside that directory are rejected before reading policy content, including
absolute paths, parent segments and symlinks; policy paths also reject
symlink/reparse aliases. Existing services launched from an unrelated
directory must update their working directory; no grant or memory migration is
needed. Programmatic policy loading retains its explicit trusted-operator API.

Enrollment records a durable binding under the Owner account's
`~/.qdrant-memory-owners`, keyed by the existing destination namespace. All updated
Runtime lease entry points consult it. Ordinary private writers take a shared
account destination lock; a protected Owner takes the exclusive form. An active
private writer therefore blocks enrollment, and a persisted claim prevents later
ordinary CLI/provider/migration admission to that destination, even after shutdown.
Different physical endpoint spellings that are not canonical URL aliases, different
OS users, old binaries and direct database clients remain outside this local check.

The registry follows the POSIX account database, not a client-selected HOME
environment variable. Protected enrollment requires a resolvable account and
writable private registry. Private containers without an account record, or a
read-only account home with no existing registry, retain their original profile
lease behavior. Provision the registry offline with all writers stopped before
enabling protection in such an environment. An existing unsafe registry is
rejected, never bypassed by private admission.

Do not delete claims to bypass a conflict. For deliberate profile relocation or
offline maintenance, stop all writers, restrict database credentials, back up both
state stores and the registry, inspect the opaque claim against trusted config,
then explicitly retire that claim before maintenance. Re-enroll the reviewed
Owner policy before reopening clients. There is no automatic migration, reset,
claim removal or temporary maintenance bypass.

## Identity and attribution

Each client token resolves to one `principal` that the private policy defines.
The bridge never accepts identity from tool arguments: `principal`, `owner_id`,
`agent_id`, `space_id`, `created_by` and `updated_by` are not part of the shared
tool schema and a request that carries them is rejected.

Memory provenance records two fields. `created_by` keeps the first writer of the
record. `updated_by` is the client that produced the current revision. A
duplicate add returns `SKIP` and does not rewrite either field.

Distinct attribution requires distinct tokens with distinct principals. Two
clients that share one token cannot be told apart. The policy validates that
every grant has a non-empty principal, so an empty subject cannot be written.

Authentication, authorization, memory provenance and durable operation auditing
are separate guarantees. Durable request receipts record the authenticated
principal for each admitted mutation, including `SKIP` and `DELETE` outcomes.
The sanitized stderr audit states allow or deny with a digest of the principal.
The Runtime does not provide a complete historical audit of reads, and it does
not retain a queryable per-operation history beyond those receipts.

## Diagnostic contract

`shared-doctor --policy POLICY` emits JSON with `status` and a `checks` array.
Each check has a stable `code`, `status` and sanitized `message`. Status values:
`PASS`, `FAIL`, `UNVERIFIED`; exit codes respectively 0, 1, 2, with FAIL taking
precedence. Policy/config parse failure produces a structured `configuration`
failure. Other codes include `policy`, `destination`, `owner_state`, `recovery`,
`local_ownership`, `local_writer`, `database_schema`, `loopback_design`,
`service_readiness`, `credential_isolation` and `backup`. `loopback_design`
states a code invariant about the supported bind address. It does not inspect a
running listener.

No tokens, credential hashes, memory text, endpoint URLs or profile paths are
included. Diagnostics never acquire a write session, recover work, initialize a
collection or test writes. A quiescent ledger is inspected with immutable read-only
SQLite; a WAL makes recovery UNVERIFIED. Remote database checks read collection
schema and the existing embedding identity only. Embedded database compatibility
is checked by normal Owner startup, not by opening embedded storage in the doctor.
A free local lock is not service health evidence. Backup coherence and effective
database key scope remain UNVERIFIED until separately established by operators.

## A. Local embedded: one user, multiple local clients

Use one private dedicated Owner home with an exclusive embedded storage path.
Initialize and enroll it as above. Do not put the profile on a shared NAS mount.
Run exactly one loopback service under its dedicated account. Provision separate
high-entropy client tokens; store only SHA256 hashes in the grants policy. Set one
grant's permission to `read` and the others to `write` as needed. Client processes
receive only their own token environment and the literal `http://127.0.0.1:PORT`
Owner URL; use `shared-mcp` for stdio MCP. Never pass database secrets to clients.
Stop clients, drain and stop the Owner before backup or upgrade. Restart with the
same policy/home; Runtime recovery and shared plan recovery finish before readiness.

## B. Self-hosted Qdrant Server

Enable API authentication and TLS. For granular collection access, Qdrant supports
JWT RBAC from 1.9; configure `service.api_key` and `service.jwt_rbac: true`.
Keep the signing/admin key away from the Owner and all agents. Issue the Owner a
JWT granting `rw` only to its dedicated memory collection; other application keys
must omit that collection or grant only separately approved reads. A read-only
instance key is read-only across collections, not a collection-restricted key.

Collection creation requires global access. Pre-provision the collection and its
compatible Runtime identity/indexes in a separate controlled administrative setup
step, then remove the administrative key from the Owner environment before
starting the long-running service. Do not broaden the Owner key to make `init`
create collections. Owner startup validates an existing collection with `create=False`.

Use `protected-server`, HTTPS and environment-injected Owner credentials. Permit
Qdrant access only from the Owner/admin maintenance network. Do not expose peer
port 6335 to agents or the Internet; isolate the cluster network as well. Mount
Qdrant data separately from Owner state. Runtime cannot determine exclusive key
distribution or rule out administrative access. An instance-wide shared admin key
does not satisfy collection-level write isolation.

## C. Synology NAS / Docker

The current private API is loopback-only. Two isolated containers cannot connect
to each other's loopback. Changing its bind to `0.0.0.0` is not a supported solution.
For a working current deployment, run Qdrant in one container on a private internal
network, and run the Owner plus its local stdio bridge processes in a second
container/network namespace. Agent hosts must launch bridges inside that Owner
namespace through an appropriately controlled local execution mechanism. A remote
agent connection to the Owner API is not supported.

Do not publish Qdrant ports to the LAN. Inject an Owner collection key through a
private secret file/environment; keep admin credentials in a separate operator
context. Give Qdrant and Owner distinct durable volumes and non-root UIDs. No
agent container may mount Owner state or Qdrant storage. Use a restart policy such
as `unless-stopped`, a private umask and readiness-aware bridge startup. Stop and
drain before upgrading; pin reviewed images and preserve volumes and claims.
Back up both volumes while quiescent and test restore. A Compose example with
independent agent containers calling loopback would not work, so none is offered.
Authenticated cross-container transport requires a separate v0.5.0 design:
explicit transport trust boundary, TLS, endpoint verification, credential lifecycle,
readiness, denial tests and a compatible client migration plan.

## D. Qdrant Cloud

Create a dedicated memory collection and cluster database key scoped to that
collection's read/write operations. Inject it only into the Owner. Use
`protected-cloud` and HTTPS. Keep Cloud management credentials and administrator
keys separate; Cloud console roles and database data access are different layers.
Apply available network restrictions. Issue no direct write key to agent clients.
Rotate by creating a replacement scoped key, stopping/draining Owner, replacing
the private secret, restarting and validating, then revoking the old key. Never
assume removing a console user revokes previously created database keys.

Cloud collection permissions, network controls, snapshots and admin access are
Cloud configuration properties, not Runtime guarantees. Preserve Owner Ledger
backups alongside the corresponding Cloud snapshot and document restore ordering.

## Backups and validation

Stop admission, drain requests and stop Owner. With every other database writer
excluded, capture a consistent Qdrant snapshot (or stopped embedded directory),
the complete Owner state including SQLite/WAL where applicable, config, private
policy and account registry. Encrypt backups and restrict access. Restore the
matching pair, correct ownership, validate policy/schema and recover before
admitting clients. Restoring only Qdrant or only Ledger can invalidate receipts
and replay assumptions. Test restores in an isolated destination before production.

The opt-in `tests/security/test_shared_security_live.py` gate requires
`QDRANT_MEMORY_SECURITY_LIVE=1`, `QDRANT_MEMORY_SECURITY_URL` (HTTPS),
`QDRANT_MEMORY_SECURITY_COLLECTION` (new `qm_security_` collection), and
`QDRANT_MEMORY_SECURITY_OWNER_KEY`, `QDRANT_MEMORY_SECURITY_READ_KEY`,
`QDRANT_MEMORY_SECURITY_UNRELATED_KEY`. Setting these explicitly authorizes creation
and deletion of that disposable collection only. Never supply a production
collection. The gate requires Owner and shared-client writes to succeed and absent,
read-only and unrelated credentials to receive 401/403. Ordinary CI skips this
gate as UNVERIFIED; skipping cannot establish database isolation.

The gate additionally requires `QDRANT_MEMORY_SECURITY_BOOTSTRAP_KEY`, held only
by the test's separate provisioning client for disposable collection creation and
cleanup. The service receives only the collection-scoped Owner key. To provision
a completely disposable local TLS/JWT fixture without production credentials:

```sh
python scripts/test_shared_security_docker.py "$QDRANT_TEST_IMAGE"
```

The image must already be installed; the runner never pulls images. Use the
digest pinned in `.github/workflows/shared-security.yml` for reproducible runs.
It generates
short-lived fixture keys and a one-day test certificate, binds only loopback,
removes its container and temporary credentials, and prints no keys. Select a
reviewed image compatible with the installed qdrant-client for your test matrix.

## Official references

- [Qdrant security and granular authentication](https://qdrant.tech/documentation/security/)
- [Database access control](https://qdrant.tech/documentation/production-operations/access-control/)
- [Cloud database keys](https://qdrant.tech/documentation/cloud/authentication/)
- [Self-hosted security tutorial](https://qdrant.tech/documentation/tutorials-operations/secure-qdrant/)

These official sources were checked on 2026-10-19. The official Qdrant MCP server
provides persistent Qdrant-backed access. This project's distinction is its memory
lifecycle, fixed scoped authorization, durable recovery and controlled shared
write path; no claim is made that the official server is insecure.
