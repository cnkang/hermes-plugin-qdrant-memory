# Qdrant Memory for Hermes

[English](README.md) | [简体中文](README.zh-CN.md)

Standalone native `MemoryProvider` for Hermes Agent. Hermes performs extraction
through its trusted `ctx.llm` facade; Qdrant stores named dense vectors and schema-v1
payloads. No Mem0 SDK, separate LLM SDK, telemetry, or core modifications.

## Install

Requirements: Python 3.11+, a compatible Hermes installation, and a reachable
embedding service. The minimum complete Hermes contract is v2026.9.24
(`f97608f178d1ffeca59860195ab7da295f7c8e5f`); CI also tests pinned
`4787e4d56fc8d9265d4c7d3c0fe5accee86b4078` and reviewed upstream main.
The default model needs Ollama and enough local resources
to serve `qwen3-embedding:4b`.

Choose the **active profile's** home explicitly; do not reuse another profile's data.

```bash
export HERMES_HOME="/absolute/path/to/your/hermes-profile"
mkdir -p "$HERMES_HOME/plugins"
git clone \
  https://github.com/cnkang/hermes-plugin-qdrant-memory.git \
  "$HERMES_HOME/plugins/qdrant-memory"
ollama pull qwen3-embedding:4b
# Start Ollama if it is not already running; keep its service available.
hermes memory setup
hermes config set memory.provider qdrant-memory
hermes qdrant-memory init
```

`hermes memory setup` prepares declared dependencies through Hermes PM. Do not
pip-install into a managed Hermes environment. Hermes versions supporting
repository installation can use
`hermes plugins install https://github.com/cnkang/hermes-plugin-qdrant-memory`.

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

Similarity is a candidate-selection signal, not proof that two facts are identical.
`SAME` skips, `SUPERSEDES` updates, `CONFLICT` creates a linked record, and
`UNRELATED` adds a record. Migration uses source IDs instead of semantic merging.

The four agent tools are `qdrant_memory_search`, `qdrant_memory_add`,
`qdrant_memory_update`, and `qdrant_memory_delete`. Maintenance stays in the CLI:

```bash
hermes qdrant-memory status
hermes qdrant-memory init
hermes qdrant-memory doctor
hermes qdrant-memory stats
hermes qdrant-memory verify
hermes qdrant-memory retry
```

`status` is local-only. `doctor` and `verify` probe existing collections without
creating or repairing them. Run `init` after first-time configuration to probe
embeddings, create the collection, record its pipeline fingerprint and build
Server/Cloud payload indexes. It makes no LLM calls and writes no conversation
memories. Repeated runs validate the existing collection; incompatible dimensions,
distance or fingerprint fail without overwriting existing data.
When the collection exists, `init` asks for `use` (the default on Enter) or `clear`.
Reuse validates the named dense vector, dimensions, distance and pipeline identity,
then checks every stored memory's payload, content hash and vector. Validation failure
returns a nonzero exit code;
a nonempty collection without a trusted fingerprint cannot be reused.
Clear probes embeddings first, then deletes and rebuilds the target collection and
discards that destination's local replay work and migration records. Other destinations
and transcript provenance quarantine are retained. Clearing is irreversible and
deletion/recreation is not atomic; rerun `init` if remote recreation fails.
For scripts, explicitly pass `--existing use` or `--existing clear`; the latter
authorizes deletion. `--collection NAME` overrides only this command's target,
not the collection configured for subsequent agent sessions.
`retry` requeues failed events for the next provider
startup and retries prepared operations. Stop the agent before maintenance of an
embedded store: Qdrant's local persistence permits only one client process.

## Configuration and migration

Behavior lives in `$HERMES_HOME/qdrant-memory.json`; credentials belong in the
active profile's Hermes secret scope. Defaults use embedded Qdrant. For server or
Cloud, set `qdrant.mode`, collection and endpoint; Cloud requires HTTPS and a
Database API key. [Configuration examples and defaults](docs/configuration.md)
cover Ollama, OpenAI-compatible embeddings, LLM routing and explicit fallbacks.

Use a separate target collection and keep the Mem0 source unchanged:

```bash
hermes qdrant-memory migrate mem0 --source-json /path/to/export.json --dry-run
hermes qdrant-memory migrate mem0 --source-json /path/to/export.json \
  --target-collection hermes_qdrant_memory --resume --verify
```

Re-embedding is the default. `--resume` needs the same snapshot and pipeline;
add `--retry-failed` to retry failed operations from that manifest. Migration
verification checks exact IDs, scope and payload hashes, not just record count.
Read the [migration guide](docs/migration-from-mem0.md) before importing.

## Operations and security

The private SQLite ledger contains raw turn events and prepared payloads. Treat
it as sensitive memory data and back up the ledger together with the embedded
store while the agent is stopped. Failed work stays in the ledger; do not delete
`state.db` to recover. Changing embedding identity requires a new collection.

Tool arguments cannot override user/agent scope. Automatic writes exclude bot
and non-primary-agent turns. Recalled content is untrusted data. See
[security](docs/security.md), [operating procedures](docs/operations.md),
[troubleshooting](docs/troubleshooting.md) and [architecture](docs/architecture.md).

## Compatibility and scope

Version 0.1 implements dense retrieval. Hybrid sparse vectors, RRF/DBSF, reranking
and recency weighting remain future capabilities; unsupported config is rejected.
Named `dense` vectors preserve a path to future sparse vectors.

Embedding `inherit` is feature-detected and requires an explicit
`inherit_fallback`. The future host facade must provide `dimensions`,
`fingerprint`, `embed_documents()` and `embed_query()`. Current Hermes has no
documented global embedding facade.

Compatibility requires Hermes `MemoryProvider`, checkpoint v2, trusted turn authors,
authoritative builtin `previous_content`, scoped secrets and context threads.
Earlier tags have incomplete contracts; v2026.9.24 is the minimum supported release. See
[validation](docs/validation.md) for tested environments and remaining live-service
checks. Read [configuration](docs/configuration.md), [migration](docs/migration-from-mem0.md),
[security](docs/security.md) and [operations](docs/operations.md) before deployment.

## Development

Run Ruff lint and format checks before committing, and install the repository
pre-commit hook as described in [development setup](docs/development.md). The hook
checks the staged snapshot. CI runs lint, both Python versions and Snyk in
parallel; SonarCloud follows coverage collection.

Build isolated dependency environments with Hermes PM, then run the host's
canonical test runner against this repository's tests. The tests import the real
host and use a temporary `HERMES_HOME`.

```bash
cd /path/to/hermes-agent
python -m pm.build_env --source /path/to/hermes-agent --group test \
  --export-requirements /tmp/hermes-qdrant-test-requirements.txt
python -c 'import sys, tomllib; from pathlib import Path; p = tomllib.loads(Path(sys.argv[1]).read_text()); print("\n".join(p["project"]["dependencies"] + p["dependency-groups"]["test"]))' \
  /path/to/hermes-plugin-qdrant-memory/pyproject.toml >> /tmp/hermes-qdrant-test-requirements.txt
python -m pm.build_env --out /path/to/hermes-plugin-qdrant-memory/.test-env \
  --requirements /tmp/hermes-qdrant-test-requirements.txt
HERMES_PYTHON=/path/to/hermes-plugin-qdrant-memory/.test-env/bin/python \
  scripts/run_tests.sh /path/to/hermes-plugin-qdrant-memory/tests -j 1 --file-retries 0
```

The plugin's `test` development dependency group is read from `pyproject.toml`.
The tested host PM exports runtime requirements only, so append the plugin runtime
dependencies and test group before building the combined environment.

Run the real embedding pilot from the plugin checkout with the host import path:

```bash
PYTHONPATH=/path/to/hermes-agent .test-env/bin/python scripts/evaluate.py
```

It uses a temporary embedded collection and UTF-8 JSON dataset. A custom dataset
contains `memories` objects with `id`/`text`, and `queries` with `query`/`relevant_ids`.
The recorded 12-topic pilot is a regression baseline, not a production recall claim.

CI runs tests, SonarCloud quality gates and Snyk dependency/code scans. An always-run
required gate rejects failed, cancelled or skipped scans without exposing tokens to
fork code. CodeRabbit is a separate GitHub App and may require manual review triggers.
See [CI service setup](docs/ci.md) and [validation evidence](docs/validation.md).

## License

MIT. Copyright © 2026 Kang Liu. See [LICENSE](LICENSE).
