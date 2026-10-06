# Qdrant Memory for Hermes

Standalone native `MemoryProvider` for Hermes Agent. Hermes performs extraction
through its trusted `ctx.llm` facade; Qdrant stores named dense vectors and schema-v1
payloads. No Mem0 SDK, separate LLM SDK, telemetry, or core modifications.

## Install

Clone this repository as `qdrant-memory` under the **active profile's** plugin
directory (`$HERMES_HOME/plugins/qdrant-memory`). Use `hermes plugins install
https://github.com/cnkang/hermes-plugin-qdrant-memory` when your Hermes version
supports repository installation. `hermes memory setup` prepares declared Python
dependencies through Hermes PM; do not pip-install into a managed Hermes environment.

For local development, symlink the repository into a disposable profile:

```bash
mkdir -p "$HERMES_HOME/plugins"
ln -s /path/to/hermes-plugin-qdrant-memory "$HERMES_HOME/plugins/qdrant-memory"
hermes memory setup
hermes config set memory.provider qdrant-memory
```

Restart the conversation after configuration changes. Existing system prompts
and toolsets stay fixed for prompt caching. Packaged installations expose the
`hermes_agent.memory_providers` entry point and keep `cli.py` and `config_schema.py`
next to the package entry point.

Defaults use embedded Qdrant and Ollama `qwen3-embedding:4b` (2560 dimensions).
Run the embedding service and provision that model before activating the provider.
For server/Cloud, configure the mode in `$HERMES_HOME/qdrant-memory.json` and supply
`QDRANT_URL` / `QDRANT_API_KEY` in the active profile's secret scope.

## Capabilities

- Embedded, self-hosted server and Cloud using the same Qdrant client.
- Ollama `/api/embed` and OpenAI-compatible `/v1/embeddings`, with strict probe,
  vector-count/dimension validation and pipeline fingerprint checks.
- Main LLM inheritance, plugin auxiliary task routing and operator-trusted overrides.
- User/agent scope filtering for recall, exact-ID update and deletion.
- Nonblocking turn persistence, serialized writes, cached session recall and
  builtin memory mirroring using authoritative `previous_content`.
- SQLite WAL event/operation ledger, transport retries and restart recovery.
- Source-ID-only Mem0 JSON/Qdrant migration, re-embedding, dry runs, resumable
  manifests, supplemental updates, and verification of IDs and payload hashes.

The four agent tools are `qdrant_memory_search`, `qdrant_memory_add`,
`qdrant_memory_update`, and `qdrant_memory_delete`. Maintenance stays in the CLI:

```bash
hermes qdrant-memory status
hermes qdrant-memory doctor
hermes qdrant-memory stats
hermes qdrant-memory verify
hermes qdrant-memory retry
```

`status` is local-only. `doctor` and `verify` probe existing collections without
creating or repairing them. `retry` requeues failed events for the next provider
startup and retries prepared operations. Stop the agent before maintenance of an
embedded store: Qdrant's local persistence permits only one client process.

## Compatibility and scope

Version 0.1 implements dense retrieval. Hybrid sparse vectors, RRF/DBSF, reranking
and recency weighting remain future capabilities; unsupported config is rejected.
Named `dense` vectors preserve a path to future sparse vectors.

Embedding `inherit` is feature-detected and requires an explicit
`inherit_fallback`. The future host facade must provide `dimensions`,
`fingerprint`, `embed_documents()` and `embed_query()`. Current Hermes has no
documented global embedding facade.

Compatibility targets the current Hermes `MemoryProvider`, `PluginContext`,
scoped secret and context-thread contracts. No minimum release tag is claimed
until the interfaces have been checked against tagged releases. See
[validation](docs/validation.md) for tested environments and remaining live-service
checks. Read [configuration](docs/configuration.md), [migration](docs/migration-from-mem0.md),
[security](docs/security.md) and [operations](docs/operations.md) before deployment.

## Development

Build isolated dependency environments with Hermes PM, then run the host's
canonical test runner against this repository's tests. The tests import the real
host and use a temporary `HERMES_HOME`.

```bash
python -m pm.build_env --source /path/to/hermes-agent --group test \
  --export-requirements /tmp/hermes-qdrant-test-requirements.txt
python -m pm.build_env --out /path/to/hermes-plugin-qdrant-memory/.test-env \
  --requirements /tmp/hermes-qdrant-test-requirements.txt \
  --requirement 'qdrant-client>=1.15,<2' --requirement 'httpx>=0.28,<1'
HERMES_PYTHON=/path/to/hermes-plugin-qdrant-memory/.test-env/bin/python \
  /path/to/hermes-agent/scripts/run_tests.sh /path/to/hermes-plugin-qdrant-memory/tests
```
