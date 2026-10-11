# Catalog integration acceptance

Maintainer-run acceptance of the `qdrant-memory` entry in the official Hermes
Plugin Catalog (upstream submission:
[NousResearch/hermes-agent PR #136024](https://github.com/NousResearch/hermes-agent/pull/136024),
merged 2026-10-10; catalog pin `5ec9cac094e5e9dc80e1cd25b241bcec44019d5b`).

Run: 2026-10-11, on a fresh isolated profile with embedded Qdrant and local
Ollama. The acceptance question: can a first-time user find the plugin in the
catalog, install it by name, configure and initialize it, and recall a stored
fact in a new session?

## Test environment

| Item | Value |
| --- | --- |
| Date | 2026-10-11 |
| Host | macOS arm64 (24 GB) |
| Python | 3.14.7 (host runtime; plugin requires 3.11+) |
| Hermes | v0.21.6+509.gf925b01 (stable, git install; base release v2026.9.24) |
| Plugin | v0.1.0, installed at the catalog pin `5ec9cac094e5e9dc80e1cd25b241bcec44019d5b` |
| Catalog | live `plugin-catalog.json` (generated 2026-10-11); entry `qdrant-memory`, tier `community`, category `memory` |
| Qdrant | embedded (`<HERMES_HOME>/qdrant-memory/qdrant`) |
| Embeddings | local Ollama `qwen3-embedding:4b`, 2560 dimensions |
| LLM | the host's configured session model; extraction and relation checks inherit it (`llm.mode: inherit`) |
| Profile | fresh isolated `HERMES_HOME`, no development shims |

## Acceptance matrix

| Item | Status | Evidence |
| --- | --- | --- |
| Catalog discoverable | PASS | `hermes plugins search qdrant-memory` (human and `--json`) and `hermes plugins browse` list the entry in a fresh home; the live catalog JSON carries the pin; `hermes plugins search qdrant` returns the separate `qdrant` plugin as well, and the two entries do not collide. |
| Install by catalog name | PASS | `hermes plugins install qdrant-memory` resolved the curated entry and checked out the pinned commit (detached HEAD at `5ec9cac…`). Interactive runs ask the provider-activation and Python-dependency consent questions; `--yes-deps` answers the dependency question for automation, and a non-interactive run without it installs the plugin disabled without preparing dependencies. |
| Catalog provenance | PASS | `hermes plugins list` / `--json` report `source: catalog:community@5ec9cac0`; `plugins/.install-metadata.json` records the catalog pin with `pinned: true`; the checkout carries a `.hermes-catalog.json` sidecar. |
| Plugin enable | PASS | `hermes plugins enable qdrant-memory` resolved and installed the declared dependencies through Hermes PM (`qdrant-client 1.19.1`, `httpx`, `portalocker` in a managed environment) and persisted across processes. |
| MemoryProvider registration | PASS | `hermes memory status` shows `qdrant-memory` installed, available and active; `hermes plugins doctor qdrant-memory` passes runtime discovery, manifest parsing, import and registration; sessions used the provider for extraction, recall and the agent tools. |
| Embedding model probe | PASS | `init` probes the embedding endpoint (`embedding_probe: OK`); an independent Ollama `/api/embed` call returned 2560 dimensions; the pipeline fingerprint is recorded (`8b5dffff…`). |
| Qdrant initialization | PASS | First `init` created the embedded collection (`action: create`, `ok: true`). A re-run without `--existing` refuses non-interactively (`existing_collection_choice_required`, exit 1); the interactive prompt defaults to `use`; `--existing use` re-validates payloads and vectors. |
| Real memory write | PASS | A real session stored two facts (a test codename and an editor preference) through the agent tool path; the background extraction also ran (relation checks recorded SKIP decisions for the duplicates) and the ledger shows COMMITTED operations for the scope only — no duplicate memories, no unexpected scope. |
| Cross-session recall | PASS | A new session in a separate process recalled both facts through scoped search; the recall came from stored memory, not conversation context. |
| Restart recovery | PASS | Memory survived multiple cold process restarts with the same collection fingerprint; `verify` reports `ok: true`; ledger counts stayed consistent with no duplicated or unexpectedly replayed operations. |
| Update / delete | PASS | `qdrant_memory_update` replaced a memory's text at its exact ID (same point, new `updated_at`); `qdrant_memory_delete` removed the targeted memory by exact ID; a later session no longer recalled the deleted fact while the other memory stayed intact. |
| Inventory / export | PASS | `hermes qdrant-memory list` reports scope, provenance and ledger backlog counts; `hermes qdrant-memory export` produced the portable Mem0-importable JSON with the original point UUID, content hash and timestamps preserved. |
| Catalog update recognition | PASS | `hermes plugins check-updates` classifies the install as `catalog`, compares the installed revision with the pin, and reports `up to date` while the pin is unchanged; the check is read-only. |
| README install guidance | PASS (updated by this change) | Before this change the READMEs recommended only URL/tag installs. They now lead with the catalog install and keep the version-locked alternatives. |
| Upstream catalog page | PASS | `/docs/plugins/qdrant-memory` renders the entry (title, the four declared tools, README from the pinned commit `5ec9cac`, "updates when the author re-pins"); card metadata matches the live catalog JSON. |

## Observations (non-blocking)

### P3 — provider interaction with structured extraction

During the run, the configured provider intermittently rejected
structured-output requests. Hermes retries without the `response_format` field
and remembers the per-route rejection ("does not accept response_format
json_schema; sending without it"), and most extraction and relation calls
completed (latency samples and dedupe decisions were recorded). In three cases
an unrecognized 400 reached the plugin as a non-retryable `BadRequestError`;
the affected events stayed `FAILED` with their bodies retained,
`hermes qdrant-memory retry` requeued them, and the final `delete-all` fenced
them as `SUPERSEDED`. The same error class appears in pre-existing ledgers of
this project. Core user flows were unaffected throughout.

- Repro: run sessions against a provider that rejects `response_format`
  json_schema; watch `hermes qdrant-memory stats` (`events.FAILED`) and the
  host logs.
- Impact: occasional lost automatic extraction; nothing is written
  incorrectly and no duplicates appear. Manual `retry` recovers pending work,
  or route extraction to a compatible model (`llm.mode: task` or `override`).
- Follow-up: none required for the plugin; see
  [troubleshooting](../troubleshooting.md) and [operations](../operations.md).

### P3 — PM workspace edge on repeated installs (host behavior)

Installing the same plugin into multiple Hermes homes on one machine can leave
two same-named plugin-source checkouts inside one PM workspace; the second
dependency resolution then aborts with
`Two workspace members are both named 'hermes-plugin-qdrant-memory'` and that
attempt stays installed-but-disabled. A single `hermes plugins enable` retry
completed cleanly, and `hermes plugins doctor` passed afterwards. A fresh
single install never hit this.

- Repro: install the plugin into two or more homes in one environment, then
  retry the affected install.
- Impact: host-side PM state, not plugin-specific; recoverable; no data
  affected.
- Follow-up: none required here.

## Out of scope

Not exercised for this run: live Qdrant Server/Cloud destinations (embedded
mode only), OS/Python matrices outside CI, and live Mem0 migrations. Existing
CI lanes cover those; nothing skipped here is recorded as a pass.

## Reproduction (abridged)

```bash
export HERMES_HOME="<fresh home>"
hermes plugins search qdrant-memory
hermes plugins install qdrant-memory        # interactive; add --yes-deps for automation
hermes plugins enable qdrant-memory
hermes memory setup                          # or: hermes memory setup qdrant-memory
hermes config set memory.provider qdrant-memory
hermes qdrant-memory init --existing use
hermes qdrant-memory status && hermes qdrant-memory doctor
hermes chat -q "Please remember: my test project codename is ORION-7."
hermes qdrant-memory list --user hermes-user --agent hermes
hermes chat -q "Search your durable memory: what is my test project codename?"
```

## Conclusion

**READY WITH KNOWN LIMITATIONS.** The official catalog installation and the core
memory lifecycle — write, cross-session recall, restart recovery, update,
delete, inventory, export and scoped deletion — passed end to end on a fresh
profile. The known limitations are the preview scope already documented in the
READMEs and the provider-dependent extraction retry noted above.

To surface the catalog-first onboarding on the catalog page itself, a follow-up
upstream catalog SHA-bump after this change merges would be required
(`/docs/plugins/qdrant-memory` renders the README from the pinned commit).
