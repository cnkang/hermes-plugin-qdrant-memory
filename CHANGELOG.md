# Changelog

## Unreleased

- Documentation: align the README install pin with the commit the `v0.1.0`
  tag points to.

- Documentation: add a condensed demo recording and a reproducible demo
  script (`scripts/demo.sh`) to both READMEs. The recording stays in the
  repository; the text script ships with the source distribution only.

- Documentation: lead both READMEs with the pinned-release install through the
  Hermes CLI, add a short positioning section, and fix the Chinese install
  wording.
- Documentation: pin the recommended install to the release commit SHA.
  `hermes plugins install --ref` requires a full 40-character commit SHA and
  does not accept tags.

## 0.1.0

Limited technical preview; released 2026-10-09; the re-publication retargets
the `v0.1.0` tag to this record commit.

- Hermes compatibility range: minimum v0.21.5 (v2026.9.24); the required CI
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
  `main`, on pull requests and on a weekly schedule; results appear as
  code-scanning alerts.
- CI: enable grouped weekly Dependabot version updates for the Python
  dependencies and the GitHub Actions pins. Dependabot pull requests run the
  lint, test and platform jobs; the credentialed scans stay skipped by design
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
