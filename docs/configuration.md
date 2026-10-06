# Configuration

The active profile's `qdrant-memory.json` holds behavior configuration; its `.env`
holds secrets. The plugin never reads `mem0.json` at runtime. Missing settings
take the defaults declared in `qdrant_memory/config.py`; unknown fields are kept
for forward compatibility.

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
explicit. Server mode defaults to `http://127.0.0.1:6333`. Cloud requires HTTPS
and a Database API key. Explicit `qdrant.url/api_key` wins over scoped environment
values; secrets in JSON are a compatibility option and are removed by setup saves.

OpenAI-compatible embedding configuration uses `provider: openai-compatible`,
`api_key_env: EMBEDDING_API_KEY`, and `send_dimensions: false` by default. A
base URL can end in `/v1`; the adapter avoids duplicating that segment.
`document_instruction` and `query_instruction` are optional explicit prefixes.
Both are included in the fingerprint along with provider, model, endpoint,
dimension, metric, dimension-sending policy and normalization version.

An embedding `mode: inherit` requires an `inherit_fallback` object with provider,
model, base_url and dimensions. Until a compatible Hermes facade exists, the
fallback is used. Different dimensions, metrics or fingerprints require a new
collection and explicit migration; the plugin never changes a live vector schema.

LLM `mode: task` uses `auxiliary.qdrant_memory_extraction` from Hermes config.
LLM `mode: override` accepts provider/model and traverses Hermes's operator trust
gate. LLM keys and fallback routing remain entirely host-owned.

`high_similarity_threshold` and `time_decay_half_life_days` reserve future ranking
policy; they do not skip relation adjudication or delete old facts. Similarity at
or above the review threshold triggers factual relation classification even at
very high similarity. `min_score` is unset until calibrated against real data.
