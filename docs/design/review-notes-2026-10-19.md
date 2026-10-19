# Design review notes — universal cross-agent memory (2026-10-19)

This file records the results of the design review of
[universal-cross-agent-memory.md](universal-cross-agent-memory.md). Each item was
reproduced in the code before it was accepted. The design already contains the
resolutions. The notes are for contributors who build the later phases.

## Process model

- The native provider acquires an exclusive write lease when it opens a profile. The
  shared owner does the same operation for its own profile. One profile cannot have both
  roles.
- Resolution: a profile that reads shared memory operates as a client of the owner. It
  has no store, no lease and no database credentials. The configuration stops with an
  error if both roles apply to one destination.
- The write lease is local and cooperative. It cannot stop a process on a different host,
  on a different OS account, in an older binary, or a direct database writer. Thus, the
  deployment guide keeps the storage credentials on the owner only.

## Remote transport

- A boolean flag for "TLS is terminated for me" is not a control. A public listener must
  not use the security exception of the loopback code.
- Resolution: the listener binds a loopback address or a Unix socket only. A reverse
  proxy on the same host terminates TLS. In the enabled mode, the owner holds two
  listeners, the loopback API and the remote listener. It reports readiness only after
  both listeners are bound, and the shutdown drains both listeners before the lease is
  released.
- Resolution: the remote path uses a shared-only factory. This factory has no private
  runtime parameter, and the private factory stays out of the remote path. A test item
  checks the parameter.
- Receipts give write idempotency. They do not give replay protection. Credential expiry,
  revocation and session invalidation are necessary before public exposure.

## Identity and isolation

- Cross-device private memory (one logical agent, two devices) must map both credentials
  to the same private partition. A shared or project grant must not supply this access,
  because it increases the visible data.
- Rules: one (owner_id, space_id) pair has one visibility value. A private space has one
  logical agent. The administrator can revoke each device credential alone.
- The configuration load must reject the reserved shared prefix in a private scope. The
  shared read path must require the protocol marker, so legacy private records stay
  invisible.

## Bounded inputs and provenance

- The reader must limit the response body before it parses the body. It must count text
  in UTF-8 bytes, not characters. It must drop large or malformed records and stop with a
  safe result.
- A recalled item keeps its provenance: the space, the visibility, the author and the
  version. The recall cache uses the policy digest as a key. When the policy changes, the
  digest changes, and the next recall does not use the earlier entry.
- The digest cannot remove text that a host already put into the model context.
  After a revocation, the host must clear or rebuild the affected context before the next
  model call. If the implementation does not include this step, the smaller guarantee
  applies: a later recall does not find the entry. The design does not claim that
  injected text gets removed.

## Deployment

- A device behind NAT cannot receive connections. The solution is an owner that clients
  can reach. A tunnel is an optional third-party component, not a product requirement.
- One supported topology is an owner and a proxy on one host with an embedded or
  self-hosted database. In a container deployment, the owner and the proxy run in one
  container, and the socket stays inside that container. A two-container HTTPS test with
  no cloud account examines this topology.
