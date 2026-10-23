# Standalone CLI and MCP

The repository now supplies a host-independent runtime alongside its Hermes
plugin. Runtime 0.2.0 published these interfaces. Runtime 0.2.1 fixes explicit
writes of highly similar texts. Runtime 0.2.2 adds MCP SDK 1.x/2.x
compatibility. Runtime 0.5.0 adds the opt-in shared owner. The optional offline
CPU embedding and guided setup commands remain available. The following pinned
installation uses 0.5.0.
The distribution name remains `hermes-plugin-qdrant-memory`. Existing Hermes
plugin discovery and `hermes qdrant-memory` grammar are compatible.

Check [PyPI](https://pypi.org/project/hermes-plugin-qdrant-memory/0.5.0/)
for Runtime 0.5.0 availability before package installation.

```bash
python3 -m venv .venv
# POSIX; on Windows use .venv\Scripts\python.exe and the Scripts command directory.
.venv/bin/python -m pip install 'hermes-plugin-qdrant-memory[mcp]==0.5.0'
.venv/bin/qdrant-memory --help
```

`mcp` is an optional extra using the official Python MCP SDK (`>=1.30,<3`). The
ordinary CLI needs only the base dependencies. Installation is an operator
action. Loading a skill does not install the runtime or alter host settings.

## Profile configuration

`--home PATH` selects a directory containing `qdrant-memory.json` and its own
`qdrant-memory/state.db`, writer leases and embedded storage. The default is
`~/.qdrant-memory`, independent of `HERMES_HOME`, Hermes profiles and Hermes
secrets. Configuration uses the same schema and validation as the plugin.
Missing config uses defaults. Malformed JSON fails closed. Validation runs
before creating storage or contacting a service.

Create the directory and this configuration yourself, choosing the identity
and embedding pipeline intentionally. The example requires a running Ollama
service with the named model already installed:

```json
{
  "schema_version": 1,
  "scope": {"user_id": "my-user", "agent_id": "my-agent"},
  "qdrant": {"mode": "embedded", "collection": "qdrant_memory"},
  "embedding": {
    "mode": "plugin",
    "provider": "ollama",
    "model": "qwen3-embedding:4b",
    "base_url": "http://127.0.0.1:11434",
    "dimensions": 2560,
    "distance": "Cosine"
  }
}
```

Defaults use `local-user` and the no-agent scope (`agent_id: null`). Relative
Qdrant paths resolve against the home. `server`/`cloud` and `openai-compatible`
embedding settings remain supported. See [configuration](configuration.md).
Only the trusted operator environment resolves `QDRANT_URL`, `QDRANT_API_KEY`
and `EMBEDDING_API_KEY` (or configured `*_env` names). Python callers may inject
`secret_reader` into `load_standalone_config` or `open_runtime`. Empty/unset
keys never fall through to Hermes. API-key transport validation remains active.
Keep credentials out of config examples, skills, prompts and committed files.

## Scoped explicit operations

Explicit add uses exact canonical-text hash deduplication within the configured
scope, without an LLM. Different texts remain separate even at similarity 1.0.
Hermes automatic extraction retains factual relation adjudication. Exact-ID
update remains an authorized replacement. Existing schema, IDs and fingerprints
are compatible. No migration is needed. Do not change the embedding pipeline
or collection during an upgrade. Use Hermes's native installer for its plugin.

```bash
qdrant-memory --home /path/to/profile status
qdrant-memory --home /path/to/profile init
qdrant-memory --home /path/to/profile add --text 'Prefer concise explanations'
qdrant-memory --home /path/to/profile search --query 'explanation preference'
qdrant-memory --home /path/to/profile search --query 'preferences' --category preference
qdrant-memory --home /path/to/profile update --id EXACT_ID --text 'Prefer examples'
qdrant-memory --home /path/to/profile delete --id EXACT_ID
```

Success results are JSON on stdout. Failures return nonzero, sanitized JSON on
stdout and diagnostics on stderr. Exact IDs confer no authorization: update and
delete refuse memories outside the configured user/agent scope. Search/add/
update/delete expose no identity override flags. Hermes retains its own caller
scope and automatic extraction/prefetch hooks. Standalone supplies explicit
operations without automatic lifecycle extraction or a host LLM dependency.

All existing maintenance/migration grammar is available, including `status`,
`init`, `stats`, `doctor`, `verify`, `retry`, `list`, `export`, `delete-all` and
`migrate mem0`. These commands are trusted operator operations and retain their
scope-selection/confirmation/export-overwrite rules. The new standalone CLI
contains explicit `--source-json`, `--source-config` and `--output` paths. These
paths must stay within the operator invocation working directory. The CLI
resolves symlinks and parent segments before it reads content or contacts
storage. Invoke the command from an explicitly authorized source/export
directory (for example, `cd /path/to`)
to import sources outside the profile. The working directory is chosen by the
trusted operator/host launcher. It must not be derived from an untrusted file
argument. This is argument containment, not an operating-system sandbox. The
operator-selected directory tree must be trusted against concurrent hostile
filesystem changes. Canonicalization does not hold a file descriptor open or
prevent a different process replacing a validated file/directory with a symlink
before access. Enforce an external filesystem sandbox when that stronger threat
model is required. Default profile configuration and `mem0.json` remain trusted profile
inputs. Legacy `hermes qdrant-memory` filesystem behavior is unchanged.
For noninteractive reuse,
pass `init --existing use`. `--existing clear` durably clears the entire target.
See [migration](migration-from-mem0.md) for re-embedding, manifests and resume rules.

```bash
cd /path/to # Authorize this source/export directory explicitly
qdrant-memory --home /path/to/profile migrate mem0 --source-json /path/to/memories.json --dry-run
qdrant-memory --home /path/to/profile migrate mem0 --source-json /path/to/memories.json --verify
qdrant-memory --home /path/to/profile list --user my-user --agent my-agent
qdrant-memory --home /path/to/profile export --user my-user --agent my-agent --output /path/to/export.json
qdrant-memory --home /path/to/profile delete-all --user my-user --agent my-agent --dry-run
```

The runtime shares `Runtime`, `QdrantStore`, `Ledger`, `WriterLease`, durable
mutation retries, IDs, payload schema and pipeline fingerprints with Hermes.
Interrupted reset or scope deletion blocks tools until operator maintenance
resumes the recorded operation. Startup replays admitted mutation operations
before admitting new tools. Failed replay blocks startup. Pending or failed raw Hermes turn events block every standalone tool startup
until Hermes recovers them through its trusted extraction context. Standalone
does not process or change those event rows.

## Local stdio MCP

See [MCP capabilities and connection](mcp.md) for the four-tool surface, a
versioned installation, client verification and the tested agent matrix.

Initialize the chosen profile first, then configure a client to launch:

```text
command: /absolute/path/to/venv/bin/qdrant-memory
args: ["--home", "/absolute/path/to/profile", "mcp"]
```

This serves only `qdrant_memory_search`, `qdrant_memory_add`,
`qdrant_memory_update` and `qdrant_memory_delete` with the existing schemas.
The process scope comes from operator-controlled configuration. Arguments,
MCP `clientInfo` and model text cannot select another identity. No migration,
bulk deletion or reset tool is exposed. Stdout carries MCP protocol only.
Tool failures use sanitized error results. Startup failures use stderr and a
nonzero process exit. This is a local subprocess interface, not a network
server or authentication service.

The MCP process holds the writer lease for its lifetime. Standalone CLI writes,
Hermes providers and another MCP process must not overlap in the same home and
destination. Read/search CLI sessions also acquire the lease so embedded
storage and recovery are coordinated. Separate homes or machines do not share
a distributed lock. Reusing a Hermes destination requires explicit matching
scope/home/ledger and a compatible embedding fingerprint. Do not point unrelated
profiles at the same remote collection and assume simultaneous writes are safe.

## Validation boundary

`tests/standalone` preserves eight installed CLI/MCP subprocess contracts and
also exercises public Python APIs plus official in-process MCP protocol clients
for schema/path refusal and startup/cleanup failures. It uses real embedded Qdrant
and a deterministic localhost embedding HTTP service. It also covers durable
scoped CRUD/restart, foreign-ID refusal, invalid-config side-effect prevention and
writer exclusion. Reset/deletion recovery, migration/export boundaries and
official MCP client initialize/list/call are included. Subprocesses actively
forbid Hermes imports and discard Hermes environment settings. The required
standalone workflow runs this suite
on Linux, macOS and Windows with Python 3.11 and 3.14 without a Hermes checkout.
CI results determine platform certification. Configuring any MCP client is
protocol compatibility, not proof that every host/model behaves equivalently.
