# Agent experience assessment

**Status:** DESIGN ONLY for a unified client command and for new lifecycle
adapters. The observability section separates what the Runtime already reports
from what it does not.

## 1. Unified setup experience

Candidate shape:

```text
qdrant-memory setup
qdrant-memory clients list
qdrant-memory clients enable <client>
qdrant-memory clients disable <client>
qdrant-memory doctor
```

These commands do not exist beyond `setup` and `doctor`.

### What already exists

| Need | Current support | Location |
|---|---|---|
| Create a private profile | `qdrant-memory setup --profile PATH` with an explicit embedding choice | `qdrant_memory/standalone.py` |
| Verify configuration and destination | `doctor` reports payload indexes, policy status, LLM mode, embedding probe and fingerprint | `qdrant_memory/cli.py` |
| Read-only inventory and export | `list`, `export`, `stats` | `qdrant_memory/cli.py` |
| Register a client | Documented per-host commands that the user runs | `INSTALL.md`, skill `references/agent-setup.md` |
| Discover configured clients | Not present | Absent |
| Enable or disable a client | Not present | Absent |

No Runtime command writes another agent's configuration file today. Client
registration stays with each host's own CLI. That is a deliberate boundary, not
a gap.

### Assessment

`setup` and `doctor` already cover the safe half of the candidate experience. A
`clients` command would need to discover other agents and edit their
configuration. That introduces four risks:

- A wrong file path could damage an unrelated host configuration.
- A silent edit could change behavior in a profile that the user did not select.
- Version pinning would differ for each host.
- Uninstalling would need a per-host rollback that the Runtime cannot verify.

Recommendation: keep the skill and installation guide as the client-registration
path. Implement a read-only `clients list` only after a host exposes a supported,
documented discovery API. Do not implement `clients enable` without explicit
per-host authorization and a verified rollback.

## 2. Native lifecycle integration

| Host | Lifecycle integration | Entry point | Verified state |
|---|---|---|---|
| Hermes | Native `MemoryProvider` | `initialize`, `on_turn_start`, `sync_turn`, `prefetch`, `on_pre_compress` (pre-compaction checkpoint), `on_session_switch`, `on_session_end`, `on_memory_write`, `handle_tool_call`, `shutdown` in `qdrant_memory/provider.py` | Production path, covered by the test suite |
| Hermes Native Owner | Opt-in extraction service with capture grants | `qdrant_memory/native_owner.py` | Released, opt-in |
| Codex | Four lifecycle hook events (`SessionStart`, `UserPromptSubmit`, `Stop`, `SessionEnd`) plus a portability CLI | MCP repository `integrations/codex-lifecycle/`, `qdrant_memory/codex_memory.py` | Hooks are registered in configuration. The adapter README records zero installed and zero active hooks on Codex CLI 0.165.0, so observed execution is NOT VERIFIED. Handoff stays `HANDOFF_PENDING` |
| DSH | Source-preview lifecycle adapter | MCP repository `integrations/dsh/native/` (`agent/created`, `agent/pre-step`, `turn/end`, `agent/disposed`) | No real-model or desktop acceptance |
| Pi | Opt-in lifecycle adapter | MCP repository `integrations/pi/owner-memory.mjs` | Covered by an isolated lifecycle harness |
| WorkBuddy | Four MCP tools | MCP repository `integrations/workbuddy/` | Explicit tools only |

Hooks that do not exist for any host:

- Workspace change. No host reports it, and the Runtime does not infer it from a
  path.
- A distinct subagent scope. Hermes gates capture on `agent_context == "primary"`
  and skips non-primary turns. DSH skips subagent sessions. Neither creates a
  separate memory scope for a subagent.
- A pre-compaction checkpoint outside Hermes. Only the Hermes provider implements
  `on_pre_compress`. Codex and DSH have no equivalent.

Recommendation: do not add a new adapter in this cycle. Each additional adapter
adds a maintenance surface that the repository cannot test on every host.

If work resumes, use this order:

1. Hermes. The only production lifecycle path with a full test suite.
2. Pi. An isolated harness already covers the adapter.
3. Codex. Only after the registered hooks are observed to run in a real session.
4. DSH and WorkBuddy. Keep the explicit MCP tools and add no lifecycle adapter.

## 3. Memory observability

### Reported today

| Signal | Meaning | Surface |
|---|---|---|
| `points_count`, `vector_dim` | Destination size and pipeline width | `stats` |
| `ledger.events` by status | Received, pending, committed, failed or quarantined events | `stats` |
| `ledger.operations` by status | Prepared, committed, failed or superseded writes | `stats` |
| `retry_total` | Attempts across operations | `stats` |
| `ledger.dedupe` counters | `ADD`, `UPDATE` and `SKIP` decisions, plus boundary, denied-capture and quarantine-discard counters | `stats` |
| Latency and throughput samples | `search_latency_ms`, `embedding_latency_ms`, `qdrant_query_latency_ms`, `extract_llm_latency_ms`, `upsert_points_per_second` and `prefetch_cache_hit_rate`, last 512 samples per name | `stats` |
| Operation backlog | Pending and failed operation counts | `list` |
| Writer diagnostics | Worker state, queue depth, pending/failed/quarantined event counts and lock state | In-process provider API, no CLI command |
| Local configuration | Mode, collection, LLM mode, embedding settings and policy generation, read without storage access | `status` |
| Readiness | Payload indexes, policy status, LLM mode, embedding probe | `doctor` |
| Embedding fingerprint | Pipeline identity | `stats`, `doctor` |

### Not reported today

- Search attempts, successful searches and empty searches as counters. Only
  latency samples exist, so an empty search is invisible.
- Automatic capture status per turn, and whether a hook is configured, ready,
  observed or failed.
- A content-free signal that a recalled memory changed a task result.

### Distinguish the states

| State | Evidence that establishes it |
|---|---|
| Configured | The profile configuration names this provider, destination or hook |
| Ready | `doctor` reports payload indexes, the embedding probe and the fingerprint |
| Unverified | A file, a settings entry or a skill install with no tool result |
| Observed | An event row, or a tool result that this session read |
| Successfully executed | A tool call returned a success payload |
| Durably committed | A ledger event or operation row has status `COMMITTED` |

A search hit proves neither execution nor durable memory. An event receipt proves
neither admission nor commit.

### Assessment

The existing ledger already distinguishes receipt from durable commit, which is
the important boundary. A useful addition is a small, local, content-free counter
set:

| Counter | Distinguishes |
|---|---|
| `search_attempts`, `search_results`, `search_empty` | A recall attempt from a useful recall |
| `capture_configured`, `capture_observed` | Configuration from observed execution |
| `write_attempts`, `write_skipped_duplicate` | Work from a duplicate decision |

Constraints for any implementation:

- Local storage only. No external telemetry and no network reporting.
- No prompts, responses, memory text or credentials in the counters.
- The counters never imply that a search result was used.

Cost: the local ledger needs a small schema addition and a migration path. That
is a durability change and must ship in its own minor release with its own
validation. The short-term answer is to document what `stats` and `doctor`
already prove, and to avoid claiming more.
