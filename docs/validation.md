# Validation evidence

## Current rereview (2026-10-06)

Minimum complete host contract: v2026.9.24
(`f97608f178d1ffeca59860195ab7da295f7c8e5f`). v2026.9.21 lacks authoritative
builtin `previous_content`; earlier tags also lack author/context-thread features.
Local full suites use the minimum tag's native runner on Python 3.11 and pinned
`4787e4d56fc8d9265d4c7d3c0fe5accee86b4078` on Python 3.14: 19 files,
104 passed, one optional Cloud skip. CI additionally tests reviewed upstream main
`3dadeb9246f4eabeee893b128ab41aa917ce28f7` on both Python versions.

Real native setup, checkpoint v2 normalization/compression and session-manager
paths are exercised. Setup's dependency installer is stubbed; dependency install
is validated separately through PM. Tests use temporary homes and deterministic
embedding/LLM fixtures. Slow LLM, Qdrant and foreground tools cover bounded drain,
retained ownership, cleanup and restart recovery. In-flight I/O is not forcibly
cancelled. The test ruamel.yaml range accommodates both host declarations.

Live Docker Qdrant v1.15.5 passes REST/gRPC collection/identity/index creation,
scope and null-agent filtering, exact-ID authorization, add/search/update/delete,
process restart/reconnect, migration and vector/payload verification. Actual remote
legacy collections remain unchanged by source reads. The image digest is
`sha256:0fb8897412abc81d1c0430a899b9a81eb8328aa634e7242d1bc804c1fe8fe863`.
Pass `--qdrant-test-url` and `--qdrant-test-container` after the runner's explicit
`--`; arbitrary environment variables are cleared. Run serially during restarts.

A built wheel installed into an independent PM environment passes site-packages
imports, entry point, package resources, schema persistence and real CLI discovery
outside the checkout. Directory discovery is also tested. Ruff lint/format,
actionlint, PM lock checks and staged-snapshot hooks are required for delivery.
Check authenticated SonarCloud/Snyk and external review results on the delivered
SHA; configuration alone is not evidence.

Cloud was not available locally. The separate **Optional Qdrant Cloud
smoke** uses Actions URL/key/prefix secrets on repository pushes or manual dispatch
and a disposable collection. Missing
credentials explicitly skip; a skip does not establish Cloud compatibility. See
[CI setup](ci.md). The earlier Ollama synthetic pilot below was not rerun in this
review and is not a production retrieval claim.

## Historical initial implementation evidence

Validated on 2026-10-06 against Hermes commit
`4787e4d56fc8d9265d4c7d3c0fe5accee86b4078`.

- Canonical Hermes `scripts/run_tests.sh`: 64 tests passed across 10 files with
  two independent file workers.
- Tests use real Hermes imports and isolated temporary profile directories.
- Persistent embedded Qdrant survived process termination after embedded upsert
  but before ledger acknowledgment; replay retained one deterministic point.
- Migration tests preserve 1,000 source IDs and detect incorrect same-count
  target records. No user memories were migrated during development.
- Actual directory-provider discovery and CLI maintenance commands passed.
- Ruff 0.15.1 lint (including docstrings/imports), formatting and GitHub Actions
  actionlint passed. All 239 Python definitions have docstrings.
- The local pre-commit hook passed on the staged repository and rejected an
  undefined-name probe in an isolated alternate Git index.
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

On commit `389e5d172631df14c12ec20cafe053c8614591dc`, both GitHub push and PR
workflows completed successfully: Python 3.11/3.14, SonarCloud, Snyk dependency/code
scans and the required aggregate gate. Snyk's resolver needs pip in its disposable
scan environment; CI provisions it through Hermes PM. CodeRabbit's separate App
check reported manual-review skipping, not a completed review of that commit.

Docstrings cover every Python module/class/function/method, including test helpers.
Documentation coverage is checked by parsing definitions; behavior is validated
separately by the canonical runner rather than source-reading implementation tests.
