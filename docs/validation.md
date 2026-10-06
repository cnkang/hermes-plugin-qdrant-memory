# Validation evidence

Validated on 2026-10-06 against Hermes commit
`4787e4d56fc8d9265d4c7d3c0fe5accee86b4078`.

- Canonical Hermes `scripts/run_tests.sh`: 38 tests passed across 8 files.
- Tests use real Hermes imports and isolated temporary profile directories.
- Persistent embedded Qdrant survived process termination after remote upsert
  but before ledger acknowledgment; replay retained one deterministic point.
- Migration tests preserve 1,000 source IDs and detect incorrect same-count
  target records. No user memories were migrated during development.
- Actual directory-provider discovery and CLI maintenance commands passed.
- Ruff undefined/unused-name checks and GitHub Actions actionlint passed.
- Live Ollama `qwen3-embedding:4b` produced 2,560-dimensional vectors. The
  synthetic 12-query pilot achieved Recall@10 and MRR of 1.0; warmed search
  p95 was 119.23 ms. See [recorded pilot](retrieval-baseline.json). This small
  fixture is a regression baseline, not a production retrieval claim.
- Server and Cloud client configuration are contract-tested; authenticated
  remote Qdrant endpoints were not available for live integration tests.

Reproduce the retrieval pilot from an environment containing Hermes and this
plugin with `python scripts/evaluate.py`. It uses a temporary embedded database
and the local Ollama service, never the user's configured collection.

CI runs the behavior suite on Linux/Python 3.11 and 3.14. SonarCloud and Snyk
workflows require service credentials; CodeRabbit requires GitHub App access.
See [service onboarding](ci.md). Committed configuration alone does not prove
that external services are authorized or that their checks passed.
