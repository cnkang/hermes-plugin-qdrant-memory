# Changelog

## Unreleased

## 0.2.2

- Support MCP Python SDK 1.x and 2.x through their respective server handler
  APIs. Keep the four scoped tools, validation and sanitized errors consistent.
- Expand the optional MCP dependency to `mcp>=1.30,<3`. Preserve the Qdrant
  client's compatible `httpx` dependency and exercise both SDK majors in CI.
- Preserve storage schema, scope, explicit-write semantics and recovery behavior.
  No data migration is required.

## 0.2.1

- Separate explicit writes from automatic factual relation adjudication. CLI,
  MCP and Hermes deduplicate canonical text within scope without an LLM.
  Different texts are saved even at cosine similarity 1.0. Automatic extraction
  retains SAME/SUPERSEDES/CONFLICT/UNRELATED behavior.
- Clarify PyPI Runtime, metadata-only MCP distribution and catalog versions.
  Add public release notes and active-version consistency coverage.
- Preserve schema-v1, deterministic IDs, embedding fingerprint and durable
  ledger/migration contracts. No data migration or new runtime dependency.

## 0.2.0

- Prepare the first PyPI Runtime release with the existing standalone CLI and
  optional MCP extra. Add wheel/sdist acceptance and GitHub OIDC publishing.
  Preserve Hermes discovery, data schema, deterministic IDs and ledger semantics.

- Publish a deployment-focused security guide and concise compatibility evidence.
  Keep preparation records outside the published source tree.
  Retain public release notes and immutable validation references.

- Persist transcript author provenance across restart and continuation. Fail closed
  on unknown or mixed history before supplementary extraction, retain provenance
  across destination reset, and preserve explicitly attributed per-turn writes.
  Deny recall if durable author or session-transition admission fails.

- Document MCP as an additional compatibility interface for the Hermes memory
  plugin: four explicit tools, source-preview installation, client validation
  and sequential sharing limits. Record the tested merged snapshot separately
  from historical v0.1.0/catalog certification.

- Contain explicit standalone migration/export file arguments within the trusted
  invocation working directory before service or file content access, including
  symlink resolution. Preserve Hermes operator CLI behavior.
- Exercise standalone Python APIs and official in-process MCP protocol boundaries
  alongside installed subprocess acceptance, and simplify maintenance setup while
  preserving readiness, recovery and reverse-order resource cleanup.

- Add host-independent profile/runtime construction, scoped JSON CLI CRUD and
  an optional official-SDK local stdio MCP adapter sharing the durable backend
  with Hermes. Preserve distribution/provider discovery and Hermes CLI behavior.
- Add no-Hermes subprocess and official MCP acceptance, with a required six-job
  Linux/macOS/Windows standalone compatibility matrix.

- Documentation: lead both READMEs with the official Hermes Plugin Catalog
  install (`hermes plugins install qdrant-memory`). The pinned-URL and tag
  installs remain as the version-locked alternatives. The entry is listed as
  community-maintained.
- Documentation: add the catalog integration acceptance record
  ([catalog-integration-acceptance.md](docs/validation/catalog-integration-acceptance.md))
  for the fresh-profile end-to-end run.

- Documentation: align the README install pin with the commit the `v0.1.0`
  tag points to.

- Documentation: add a condensed demo recording and a reproducible demo
  script (`scripts/demo.sh`) to both READMEs. The recording stays in the
  repository. The text script ships with the source distribution only.

- Documentation: lead both READMEs with the pinned-release install through the
  Hermes CLI, add a short positioning section, and fix the Chinese install
  wording.
- Documentation: pin the recommended install to the release commit SHA.
  `hermes plugins install --ref` requires a full 40-character commit SHA and
  does not accept tags.

## 0.1.0

Limited technical preview. Released 2026-10-09. The re-publication retargets
the `v0.1.0` tag to this record commit.

- Hermes compatibility range: minimum v0.21.5 (v2026.9.24). The required CI
  matrix validates the minimum release and the latest Hermes `main`.
- Scoped deletion intents are versioned and explicit. A literal agent id `*`
  is an ordinary scope and never selects all agents. A pre-versioned intent is
  refused (`scope_delete_legacy_intent`) until resolved with
  `delete-all --resolve-legacy single_agent`, or `--resolve-legacy all_agents`
  when the recorded agent is `*` or absent. The `delete-all` result `scope`
  field reflects the versioned intent.
- `--all-agents` deletions fence prepared writes and supersede pending turn
  events in single bounded passes. Multi-scope runs recover from interruption
  without widening the recorded scope set.
- CI: add a CodeQL workflow that analyzes the Python sources and the GitHub
  Actions workflows with the `security-extended` query suite on pushes to
  `main`, on pull requests and on a weekly schedule. Results appear as
  code-scanning alerts.
- CI: enable grouped weekly Dependabot version updates for the Python
  dependencies and the GitHub Actions pins. Dependabot pull requests run the
  lint, test and platform jobs. The credentialed scans stay skipped by design
  and re-run on `main` after merge.

- Merged PR #10: durable destination reset intents and crash recovery, scoped
  deletion fences, terminal payload scrubbing, resource cleanup, platform matrix
  and separate latest-Hermes tracking.
- Merged PR #11: commit-time fences for prepared UPSERT replay across point IDs
  sharing content, retained-history lookup indexes, and installed-wheel upstream
  discovery/manifest checks.
- Merged PR #12: migration terminal-state accounting and bounded ledger replay,
  additional hard process-exit reset tests, and refreshed release evidence.
- Migration verification exposes sanitized `migration_superseded` conflicts.
  Mixed failed/superseded plans report incomplete progress before terminal conflicts.
  Progress counts use bounded primary-key lookups without reading payload bodies.
- Verification raises `migration_incomplete` with `--resume --retry-failed`
  guidance while failed or missing records remain, ahead of any superseded conflict.
- Provider discovery and registration import without runtime dependencies, so
  `hermes memory setup` works on a fresh home before Hermes prepares them.
- Memory inventory, export and scoped deletion: read-only `list` with ledger
  backlog counts and portable `export` in the Mem0-importable shape (re-import
  with `migrate mem0`). It also adds a durable scoped `delete-all` with reset-style
  intent, prepared-write fencing, `--dry-run`/`--confirm` and fail-closed
  recovery.
- Scoped-deletion hardening: `delete-all` also invalidates admitted-but-
  unprocessed turn events for the scope and supports `--all-agents` (strict runs
  report `other_agent_scopes` when other agent scopes still hold memories).
  Resumes are idempotent, and `export` streams records for large scopes.
- Release and community readiness: data-flow/privacy guidance in the READMEs,
  pinned release installs and release notes, and CONTRIBUTING and SECURITY
  policies. It also adds issue/pull-request templates, release reports organized
  under docs/releases, and package `readme`/`urls` metadata.
- Merged PRs #13 (documentation sync) and #14 (`migration_incomplete` ordering in
  verify mode). Post-merge main `f6a2e97` passed CI, the six-job platform matrix
  and executed authenticated Cloud integration, with the earlier `32bd4e7`
  snapshot retained in [validation evidence](docs/validation.md).

Initial standalone dense-memory provider: Hermes LLM inheritance, Ollama and
OpenAI-compatible embeddings, scoped Qdrant storage, and a durable
turn/operation ledger. It also includes cached recall, exact builtin mirroring,
tools, setup and maintenance CLI, and Mem0
source-ID migration with resumable verification. Hybrid/rerank and vector reuse
remain future work. Minimum complete Hermes contract: v2026.9.24.

Checkpoint v2 durably archives compression evidence. Setup uses the native schema
wizard. Reset clears session bookkeeping, bounded shutdown retains local writer
ownership during slow I/O, and stats persists dedupe decisions and active cache
samples. Migration compares physical destinations and revalidates resumed SKIPs.
CI exercises real Server REST/gRPC and minimum plus latest-main host contracts.

Pre-release hardening also fences retries of previously prepared writes after a
scoped delete, including same-content writes that use a different point ID. A
newly admitted post-delete write is still allowed. Targeted SQLite indexes keep
replay,
open-operation and delete-fence lookups efficient as idempotency history grows.
The latest-Hermes lane installs the built plugin distribution and loads its
provider entry point. It checks the manifest parser against current Hermes main
on Python 3.11 and 3.14 while recording the exact host SHA.

Includes English and Chinese READMEs, complete English Python docstrings, and
architecture and recovery guides. It also includes staged-snapshot Ruff pre-commit
checks and parallel lint/test/dependency/code CI jobs with an always-run required gate.
