# Shared Memory threat model v1

Scope: the loopback Shared Owner protocol, its Runtime/Ledger, local destination
claims and the optional protected destination policy. This model does not change
the memory schema or implement distributed fencing.

## Trust boundaries

1. **Application authorization:** fixed credential grants bind principal, owner,
   space, visibility and read/write permission. Tool arguments cannot choose them.
2. **OS/process ownership:** the profile lease excludes concurrent profile writers.
   Optional protected ownership adds an account-local destination claim and
   exclusive destination lock across cooperating Runtime profiles. POSIX ownership,
   private modes, regular files and symlink checks protect state from other users.
3. **Database access:** collection-scoped Qdrant credentials determine who can
   bypass the application. Only the Owner should hold application write access.
4. **Network isolation:** agents can reach the loopback Owner API; access to Qdrant
   is restricted independently by firewall, container network or Cloud controls.
5. **Operational guarantees:** operators provision keys, stop old writers, manage
   administrators, protect backups and restore coherent database/ledger state.

**A local lock cannot stop a remote writer with independent database credentials.
Configuration checks cannot prove that no administrator has write access.**
The account-local claim is a local admission record, never a database lock.

## Threat cases

| Actor / required capability | Existing control | Protected mode and deployment control | Detection, assumptions and residual risk |
|---|---|---|---|
| Agent with Owner client token | Fixed grants, exact tool schema, scope filters | No database credential is distributed | Authorized writes remain possible within its grant; revoke compromised tokens |
| Compromised read-only Agent | Writes rejected before planning | Separate per-agent token | Confidentiality within its allowed read space is lost; no write permission |
| Agent with unrelated Qdrant key | Runtime does not control direct database access | Qdrant per-collection permissions deny protected-collection writes | Doctor cannot infer effective key scope; verify on a disposable collection |
| Legacy private profile targeting shared collection | Profile-local lease and pending shared request interlock | Durable claim rejects ordinary Runtime lease admission after enrollment | Only updated cooperating Runtime entry points under the same OS account participate |
| Second Owner in another profile | Existing lease does not span profile homes | Exclusive account destination lock; persistent claim binds one home and fingerprint | Alternate endpoint aliases, different users and old binaries remain deployment responsibilities |
| Another machine with direct Qdrant access | No remote fencing | Owner-only collection write key, restricted network | Independent admin/write keys bypass Ledger and CAS; no application guarantee |
| Process under Owner OS user | Same account may read private state and credentials | Checks detect ordinary misconfiguration | Equivalent privileges can remove claims, change code or impersonate Owner; malicious same-user processes are outside the guarantee |
| Operator using administrative Qdrant key | Admin key works as database credential | Provision collection-scoped key and keep signing/admin secrets separate | Presence or JWT claims do not prove minimum privilege; doctor reports UNVERIFIED |
| Container on Qdrant network without key | Database auth can deny requests | Enable Qdrant authentication and scoped credentials | Network reachability alone must not authorize writes; verify deployment |
| Container sharing Owner data volume | May access ledger, policy or secrets | Mount Owner state only into Owner container; restrictive UID and permissions | A container with equivalent volume/OS access can bypass all local checks |
| Request replay / reused operation ID | Durable input-bound receipts and immutable plans | Same protocol retained | Exact replay returns receipt; changed input conflicts; retention/backup must preserve ledger |
| Attribution loss between token and record | Valid token authorizes the call, but the caller identity is dropped before persistence | Identity is resolved only from the server grant, is part of the request key, and is written to the payload and receipt | Distinct tokens per client; do not share one token across agents | A shared token makes attribution ambiguous; reads have no per-record actor history; this is not a complete audit trail |
| External overwrite of revision/payload | Stale observed versions conflict through Owner | Database write isolation prevents unauthorized direct updates where enforced | External writes that preserve revisions or race after checks cannot reliably be detected; CAS is application-level |

No protection is claimed against a malicious administrator, theft of the Owner's
complete credentials, or equivalent OS privileges. Payload protocol markers limit
which records are shared, but never establish ownership authority or promote
legacy records. A database marker would not provide atomic fencing and is not used.

## Alternatives and compatibility

Documentation alone leaves accidental cooperating local writers unchecked. A
distributed coordinator would change the architecture without removing database
administrator capabilities. The selected policy combines local fail-closed
admission with independently enforced database privileges. Existing private and
legacy shared policy formats continue to work unless they target an enrolled
protected destination. Protected mode is opt-in and currently requires POSIX
permission verification; Windows private/shared compatibility remains supported.
