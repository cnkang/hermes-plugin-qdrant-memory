# Dedicated Owner service and rollback (source preview)

The sample `docs/examples/universal-owner/qdrant-memory-owner.service` runs one standalone Owner under a dedicated Linux account.
The template uses private file permissions at startup.
It does not create a service, firewall rule, or account.
macOS launchd, Windows services, and user interfaces require separate tests.
Published Runtime 0.4.0 does not include these preview features.

## Prepare the service

1. Create the `qdrant-memory` account with home `/var/lib/qdrant-memory`.
   Set the home and state directory permissions to 0700.
   Set the configuration and policy file permissions to 0600.
   Keep the account home consistent with the protected destination registry.
2. Install the selected Runtime with its `[remote]` extra in a versioned virtual environment under `/opt/qdrant-memory`.
   Do not replace an environment while its service runs.
3. Stop all other writers before you configure the backend, embedding fingerprint, device policy, and protected destination.
   Give only this Owner process access to an Embedded directory.
   For a Qdrant server, use HTTPS and a database key with access to the required collection.
4. Store only necessary Runtime secrets in `/etc/qdrant-memory/owner.env`.
   Set root ownership and 0600 permissions on this file.
   Systemd reads this file.
   Do not put tokens in command arguments.
5. Replace the example hostname and paths.
   Install the service unit.
   Configure the TLS proxy from [Remote Owner](remote-owner.md) on the same host, ahead of port 8766.
   Keep both Owner listeners on loopback addresses.
   Check the unit before you enable it.

The sample umask applies before Qdrant and SQLite create files.
The Owner rejects existing files with unsafe permissions.
It does not change their permissions automatically.
Use an authorized offline procedure to replace unsafe state.
Do not delete lock files, ownership claims, or write-ahead log (WAL) files.
The writable path contains state and the service account's ownership registry.
The unit cannot write to other users' profiles.

## Enable lifecycle capture

The template keeps lifecycle capture off.
Configure an explicit Owner LLM override before you add `--enable-lifecycle`.
Examine the project capture rules before you enable capture.

`max_output_tokens` has a default value of 800.
It accepts an explicit integer up to 8192.
Reasoning models can need a larger value, such as 2048.
The Owner records a response that exceeds its token limit as a failed durable event.
It does not record that response as successful extraction with no facts.
A larger token limit does not establish extraction quality.
Run the fixed anonymous model evaluation separately.

## Rehearse an upgrade or rollback

Use a disposable test environment for this procedure.

1. Stop new requests at the proxy.
   Stop the service and let active work finish.
   The service allows 180 seconds for shutdown.
2. Back up the Owner configuration, policy, Ledger, and corresponding Qdrant data together.
   Preserve ownership, permissions, the embedding identity, and the schema identity.
   Do not combine a new Ledger with an unrelated database snapshot.
3. Select the required virtual environment.
   Check the configuration and ownership claims.
   Start one Owner.
   Reconcile pending and failed durable work before you reopen the proxy.
   Check scoped discovery, reads, a synthetic CAS write, denial, and idempotent retry.
4. For rollback, stop the service again.
   Restore the matching version, configuration, Ledger, and data snapshot as one set.
   If an older version cannot read the new state, restore the previous complete snapshot.
   Do not change Ledger statuses or delete ownership files.

These instructions and the service unit are examples.
They do not establish that a production installation, public deployment, or real rollback passed validation.
