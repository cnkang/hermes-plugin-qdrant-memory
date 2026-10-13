# MCP compatibility for other agents

This project primarily supplies Hermes Agent's native memory plugin. Its optional
MCP adapter makes the same durable backend available to other agents without
Hermes. Hermes keeps automatic turn extraction and prefetch; MCP provides
explicit memory operations.

## What the client receives

| Tool | Input | Behavior |
|---|---|---|
| `qdrant_memory_search` | `query`, optional `category` | Search the configured scope; return memory IDs, scores and provenance. |
| `qdrant_memory_add` | `text` | Persist an explicitly authorized fact or preference. |
| `qdrant_memory_update` | exact `id`, replacement `text` | Replace a memory after checking its existing scope. |
| `qdrant_memory_delete` | exact `id` | Delete a memory after checking its existing scope. |

The server supports MCP initialization, tool discovery and tool calls through
the official Python SDK over local **stdio**. Tools use the same store, embedding
pipeline, ledger, retry/recovery rules and writer lease as the Hermes plugin.
Only these four tools are advertised; no resources or prompts are advertised.
Maintenance, export, migration, reset and bulk deletion remain operator CLI
operations. There is no HTTP/SSE listener or remote authentication service.

## Connect a client

Runtime **0.2.0** published the MCP interface; **0.2.1** fixes explicit writes
of similar texts without an LLM. Install the following after publication.
The older v0.1.0 release and its
Hermes catalog installation do not supply this new entrypoint. Install the
versioned Runtime with the optional `mcp` extra:

```bash
python3 -m venv /private/path/qdrant-env
/private/path/qdrant-env/bin/python -m pip install \
  'hermes-plugin-qdrant-memory[mcp]==0.2.1'
```

Use Python 3.11+. On Windows, use the environment's `Scripts` executables.
Configure a private profile and reachable embedding service using
[standalone setup](standalone.md#profile-configuration), then run:

```bash
/private/path/qdrant-env/bin/qdrant-memory --home /private/path/profile init
/private/path/qdrant-env/bin/qdrant-memory --home /private/path/profile doctor
```

Choose the client's settings scope deliberately. For Codex CLI:

```bash
codex mcp add qdrant-memory -- /private/path/qdrant-env/bin/qdrant-memory \
  --home /private/path/profile mcp
codex mcp list
```

For other clients, configure the launcher with these values in that client's
settings format:

```text
command: /private/path/qdrant-env/bin/qdrant-memory
args: ["--home", "/private/path/profile", "mcp"]
```

Let the client start the process. Verify connection and discovery of all four
tools, then request an authorized search. Registration alone does not establish
a working connection. Keep stdout reserved for MCP traffic; diagnostics use stderr.
The [agent setup guide](https://github.com/cnkang/skills/blob/main/qdrant-memory/references/agent-setup.md)
covers host-specific commands. MCP works independently of the companion skill;
the skill supplies recall/edit/maintenance instructions and permission boundaries.

## Identity and shared memory

The operator selects the profile and credentials before the MCP process starts.
Tool input and client names cannot override its user/agent scope. An exact ID
does not permit access outside that scope. Protect the profile directory and
launcher configuration; stdio scope is not an internet authentication system.

For sequential sharing by the same authorized user, use the same profile,
collection, embedding pipeline and `scope: {"user_id": "my-user", "agent_id": null}`.
Explicit agent IDs remain private partitions. Close one MCP session before
opening another against that profile: the server holds its writer lease for
its entire lifetime. Independent profiles/hosts do not share a distributed lock.
This preview does not provide a concurrent multi-agent shared service.

## Evidence and limits

The [validation record](validation.md#source-preview-validation-2026-10-12)
links the tested source revision: 27 standalone contracts across Linux, macOS
and Windows with Python 3.11/3.14, including real CLI subprocesses and official
MCP clients. The companion skill also has isolated local Codex CLI, OpenCode
and Pi integration checks and shared/private scope E2E.

| Agent | Verified boundary |
|---|---|
| Hermes | Native provider/lifecycle and maintenance integration; MCP is optional. |
| Codex CLI 0.160.0 | MCP discovery and actual add/search calls through its app-server. |
| OpenCode 2.0.24 | Saved CLI registration plus explicit API connection/inventory; automatic project activation is not certified. |
| Pi 1.1.0 | CLI connection/tool discovery and SDK skill loading with a scripted model fixture. |
| Claude Code and other compatible clients | Setup guidance/protocol compatibility; no local host/model certification recorded. |

These tests establish transport, storage and scope behavior without live model
inference. They do not certify every agent/model's recall decisions. MCP does
not automatically extract conversations or replace the Hermes lifecycle.
