# Changelog

## 0.1.0

Initial standalone dense-memory provider: Hermes LLM inheritance, Ollama and
OpenAI-compatible embeddings, scoped Qdrant storage, durable turn/operation ledger,
cached recall, exact builtin mirroring, tools, setup and maintenance CLI, and Mem0
source-ID migration with resumable verification. Hybrid/rerank and vector reuse
are deferred. Minimum complete Hermes contract: v2026.9.24.

Checkpoint v2 durably archives compression evidence; setup uses the native schema
wizard. Reset clears session bookkeeping, bounded shutdown retains local writer
ownership during slow I/O, and stats persists dedupe decisions and active cache
samples. Migration compares physical destinations and revalidates resumed SKIPs.
CI exercises real Server REST/gRPC and minimum/pinned/current host contracts.

Includes English and Chinese READMEs, complete English Python docstrings,
architecture and recovery guides, staged-snapshot Ruff pre-commit checks, and
parallel lint/test/dependency/code CI jobs with an always-run required gate.
