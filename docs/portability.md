# Sharing vs Portability vs Backup

This page clarifies five related concepts. The Runtime is the authority for
these definitions. The MCP distribution and the Agent Skill link here instead of
keeping their own copies.

## Concepts at a glance

| Concept | What it means | What it is not |
|---|---|---|
| Shared Memory | Different agents reach one authorized shared space through the Shared Owner | Not automatic publication of private records |
| Memory Portability | Move durable memory data to a new storage environment through supported export/import and re-embedding | Not a full Owner state snapshot |
| Mem0 Migration | Import supported Mem0 JSON exports or supported Qdrant collections | Not automatic import from every Mem0 Cloud account |
| Backup and Restore | Recover Qdrant, the SQLite ledger, configuration and authorization material together | Not a plain JSON export |
| Harness Switching | A new harness can reach the shared memory service after configuration and authorization | Not automatic import of chat history or tool state |

## Shared Memory

Agents access the same authorized shared memory through the Shared Owner. Each
access is scoped by user, agent and space. Private records stay private unless an
operator explicitly shares them. The Shared Owner is opt-in and off by default.

- One authoritative writer per destination.
- Local WriterLease coordinates local processes. It is not a distributed lock.
- Agents cannot bypass the Owner to write shared storage directly.

## Memory Portability

Portability moves durable memory records between storage environments using the
supported export and import flow. Imports re-embed by default.

Rules for portability:

- Source record IDs are preserved where the flow supports it.
- Embeddings are regenerated for the target pipeline unless reuse is explicitly supported.
- Scope and provenance are carried with each record.
- A plain JSON export covers stored memories only. Pending events, retry history and crash-recovery state stay in the local ledger.

## Mem0 Migration

The Runtime supports migration from:

- Mem0 JSON exports.
- Supported Qdrant collection data sources.

The migration tool provides:

- Resumable manifests.
- Verification of IDs, scope and payload hashes.
- Source isolation: the source collection stays unchanged.

The tool does not claim direct import from every Mem0 Cloud account.

## Backup and Restore

A complete restore must coordinate:

- Qdrant data (vectors and payloads).
- The SQLite ledger (pending operations, retry bookkeeping, session mappings).
- Configuration (`qdrant-memory.json`).
- Required authorization material and credentials.

A JSON export is not a complete Owner state snapshot. Back up the profile home
together with Qdrant data for a full recovery point.

## Harness Switching

A new harness can use shared memory after configuration and authorization. It
does not automatically import the original harness's chat history, internal state
or tool history. Only authorized shared memory records are available.

## See also

- [Shared memory overview](shared-memory/README.md)
- [Deployment security](shared-memory/deployment-security.md)
- [Migration from Mem0](migration-from-mem0.md)
- [Operations](operations.md)
