# Configuration

The active profile's `qdrant-memory.json` holds behavior configuration. Its `.env`
holds secrets. The plugin never reads `mem0.json` at runtime. Missing settings
take the defaults declared in `qdrant_memory/config.py`. The plugin keeps unknown
fields for forward compatibility.

During `hermes memory setup`, the wizard resolves `QDRANT_URL` and `QDRANT_API_KEY`
through Hermes's active profile secret scope. Each nonempty setting skips its corresponding
prompt. The wizard still requests missing settings. The wizard reports variable
names only.
An existing environment URL supersedes a previously saved explicit URL. Setup saves
`url_env` and `api_key_env` references, without copying environment values or credentials.
Endpoint and HTTPS transport checks still apply. Deployment mode remains a user choice.

```json
{
  "schema_version": 1,
  "scope": {"user_id": "hermes-user", "agent_id": "hermes"},
  "qdrant": {
    "mode": "cloud", "collection": "hermes_qdrant_memory",
    "url_env": "QDRANT_URL", "api_key_env": "QDRANT_API_KEY",
    "timeout_seconds": 5, "prefer_grpc": false
  },
  "llm": {"mode": "inherit", "task": "qdrant_memory_extraction"},
  "embedding": {
    "mode": "plugin", "provider": "ollama", "model": "qwen3-embedding:4b",
    "base_url": "http://127.0.0.1:11434", "dimensions": 2560,
    "distance": "Cosine", "batch_size": 32, "timeout_seconds": 30
  },
  "search": {"mode": "dense", "top_k": 8, "candidate_k": 24,
    "min_score": null, "hnsw_ef": 128, "exact": false, "rerank": false},
  "dedupe": {"similarity_review_threshold": 0.92, "high_similarity_threshold": 0.97},
  "write": {"batch_size": 64, "max_attempts": 5,
    "backoff_base_seconds": 0.5, "backoff_max_seconds": 30,
    "shutdown_timeout_seconds": 5},
  "limits": {"max_text_bytes": 65536, "max_metadata_bytes": 32768,
    "max_payload_bytes": 131072, "oversize_policy": "reject"}
}
```

Embedded mode uses `$HERMES_HOME/qdrant-memory/qdrant`, unless `qdrant.path` is
explicit. Relative paths resolve against the active profile home, and canonical
storage paths determine the ledger namespace independently of the working directory.
Server mode defaults to `http://127.0.0.1:6333`. Cloud requires HTTPS
and a Database API key. Explicit `qdrant.url/api_key` wins over scoped environment
values. Secrets in JSON are a compatibility option, and setup saves remove them.

OpenAI-compatible embedding configuration uses `provider: openai-compatible`,
`api_key_env: EMBEDDING_API_KEY`, and `send_dimensions: false` by default. A
base URL can end in `/v1`. The adapter avoids duplicating that segment.
`document_instruction` and `query_instruction` are optional explicit prefixes.
The fingerprint includes both prefixes along with provider, model, endpoint,
dimension, metric, dimension-sending policy and normalization version.

An embedding `mode: inherit` requires an `inherit_fallback` object with provider,
model, base_url and dimensions. Until a compatible Hermes facade exists, the plugin
uses the fallback. Different dimensions, metrics or fingerprints require a new
collection and explicit migration. The plugin never changes a live vector schema.

LLM `mode: task` uses `auxiliary.qdrant_memory_extraction` from Hermes config.
LLM `mode: override` accepts provider/model and traverses Hermes's operator trust
gate. LLM keys and fallback routing remain entirely host-owned.

`high_similarity_threshold` and `time_decay_half_life_days` reserve future ranking
policy. They do not skip relation adjudication or delete old facts. Similarity at
or above the review threshold triggers factual relation classification even at
very high similarity. `min_score` is unset until calibrated against real data.

## Minimal deployment examples

The plugin merges defaults recursively, so embedded mode needs only the settings you change:

```json
{
  "qdrant": {"mode": "embedded"},
  "scope": {"user_id": "your-user-id", "agent_id": "hermes"}
}
```

For an existing self-hosted server:

```json
{
  "qdrant": {
    "mode": "server",
    "url": "https://qdrant.example.com",
    "collection": "hermes_qdrant_memory"
  }
}
```

For OpenAI-compatible embeddings, replace the endpoint/model/dimension placeholders
with the service's actual pipeline contract. The example dimension is illustrative:

```json
{
  "embedding": {
    "mode": "plugin",
    "provider": "openai-compatible",
    "model": "your-embedding-model",
    "base_url": "https://embedding.example.com/v1",
    "dimensions": 1024,
    "api_key_env": "EMBEDDING_API_KEY",
    "send_dimensions": false
  }
}
```

Supply EMBEDDING_API_KEY through Hermes's profile secret handling. Enable
`send_dimensions` only when that API/model supports the parameter. Startup checks
the returned dimension and fingerprint. A changed model with the same dimensions
still requires a compatible new collection.

LLM routing is independent of embedding:

```json
{"llm": {"mode": "task", "task": "qdrant_memory_extraction"}}
```

Configure the registered task using Hermes auxiliary-model setup. For an explicit
operator-authorized provider/model selection:

```json
{"llm": {"mode": "override", "provider": "your-provider", "model": "your-model"}}
```

These plugin settings do not bypass Hermes trust grants or contain LLM credentials.

## Storage, limits and scope

| Setting | Default | Contract |
| --- | --- | --- |
| qdrant.collection | hermes_qdrant_memory | A separate plugin-managed collection with named dense vectors |
| qdrant.path | Active home/qdrant-memory/qdrant | Relative paths resolve against active home; keep distinct across profiles |
| scope.user_id / agent_id | hermes-user / hermes | Explicit gateway authors take precedence for their turns; agent may be null |
| search.top_k / candidate_k | 8 / 24 | Candidate count must be at least result count |
| write.max_attempts | 5 | Total attempt budget, not five retries after the first attempt |
| write.shutdown_timeout_seconds | 5 | Drain deadline; timeout raises and retains writer ownership until in-flight I/O exits |
| limits.max_text_bytes | 65536 | UTF-8 text size; optional explicit text truncation |
| limits.max_metadata_bytes | 32768 | Metadata oversize is always rejected |
| limits.max_payload_bytes | 131072 | Full payload oversize is always rejected |

The plugin accepts explicit JSON Qdrant credentials for compatibility, but setup
saves remove them. Prefer the profile's secret scope for all keys. Keep embedded paths
distinct across profiles. Writer leases live under each `HERMES_HOME`: separate
profiles do not coordinate ownership even on the same machine. Run only one writer
per destination across all profiles and hosts, including Server/Cloud. Stop all
of them before mutating maintenance commands. A shared absolute storage path does
not provide safe cross-profile ownership.
Changing scope does not automatically migrate or grant access to another scope's records.
