# Dedicated Owner service and rollback (source preview)

The sample `docs/examples/universal-owner/qdrant-memory-owner.service` runs one
standalone Owner as a dedicated Linux account with private startup permissions.
It is a template: no service, firewall or real account is created automatically.
macOS launchd and Windows service/UI acceptance remain separate verification.

Before installation, the operator must:

1. Create the `qdrant-memory` account with home `/var/lib/qdrant-memory`; restrict the
   home/state directories to that account (0700) and configuration/policy to 0600.
   Its account home must match the protected destination's account-local registry.
2. Install the reviewed Runtime `[remote]` optional extra into a versioned virtualenv
   under `/opt/qdrant-memory`; do not replace a running environment in place.
   Source preview features are absent from published Runtime 0.4.0.
3. Prepare the explicit backend config, embedding fingerprint, device policy and
   protected destination enrollment with all competing writers stopped. Use an
   Embedded directory only from this Owner process, or an HTTPS Qdrant endpoint
   with the appropriate collection-scoped database key.
4. Store only needed runtime secrets in `/etc/qdrant-memory/owner.env` with root-only
   0600 permissions. Systemd reads this file; do not place tokens in command arguments.
5. Replace the sample hostname/path settings, install the service unit, and put the
   same-host TLS proxy from `remote-owner.md` in front of port 8766. Neither Owner
   listener binds publicly. Validate the unit locally before enabling it.

The sample umask applies before Qdrant/SQLite create files. Existing exposed files
are rejected; the Owner does not silently chmod them. Re-provisioning exposed state
requires an authorized offline procedure, never deleting lock/claim/WAL files.
The writable path contains both state and the service account's ownership registry;
the unit intentionally cannot write into other users' profiles.

Lifecycle capture stays off in this template. To opt in, configure an explicit
Owner LLM override and audit project capture rules before adding `--enable-lifecycle`.
`max_output_tokens` defaults to 800 and accepts an explicit integer up to 8192;
thinking models may need a larger budget, such as 2048. A token-truncated response
is a failed durable event, never an empty successful extraction. A larger budget
does not prove extraction quality; run the fixed anonymous diagnostic separately.

For an upgrade/rollback rehearsal in a disposable environment:

1. Stop admitting new requests at the proxy and stop the service; let active work
   drain. The 180-second stop timeout is deliberately not a short worker kill.
2. Back up Owner config, policy, Ledger and the corresponding Qdrant data together,
   preserving ownership/modes and the embedding/schema identity. Do not combine a
   new Ledger with an unrelated database snapshot.
3. Select a reviewed virtualenv, validate config/claims, start the single Owner and
   reconcile pending/failed durable work before reopening the proxy. Verify scoped
   discovery/read, a synthetic CAS write, denial and idempotent retry.
4. If rolling back, stop again and restore the matching version/config/Ledger/data
   snapshot as one set. If an old version cannot read a new feature's state, restore
   the prior complete snapshot; do not edit statuses or delete ownership files.

These steps and the unit's CLI grammar are reviewed examples, not evidence of a
completed systemd production installation. No public deployment or real rollback
has been performed by this development task without explicit authorization.
