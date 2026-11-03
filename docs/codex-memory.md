# Codex memory portability adapter

One Memory. Multiple Agents. No Lock-in.

Users own their memory. Import, export, documented formats and safe exit are
product requirements. No account, plugin installation or proprietary decoder is
needed to read an export. New harness adapters belong here, in the Runtime,
and must use a harness's documented interface when one is available. Current
adapters cover Mem0 JSON/collections, Runtime JSON and Codex semantic artifacts.
This is not a claim that every harness has a supported native write API.

## Compatibility evidence (2026-11-03)

Inspected openai/codex main `616cab3a3529f572c3385098c12698278770ea98`:

- `codex-rs/config/src/types.rs`: `MemoriesToml.version`, `dual_write`,
  `generate_memories`, `use_memories`, `dedicated_tools`.
- `codex-rs/memories/write/templates/memories/consolidation_v2.md`: v2 writes
  `memory_summary.md`. v2 is a consolidated summary, not a v1 MEMORY.md registry.
- `codex-rs/memories/write/src/storage.rs`: raw memories and rollout evidence
  are generated inputs. This adapter deliberately excludes them.
- `codex-rs/ext/memories/src/local/ad_hoc_note.rs`: an internal note backend
  exists; it is not a stable external bulk-import API.
- `codex-rs/app-server-protocol/src/protocol/v2/memory.rs`: thread read/status
  do not constitute a public bulk-import interface.

See [official memory behavior](https://developers.openai.com/codex/memories).
CLI 0.165.0 was available for verification. IDE and Desktop effective settings
may differ from the TOML file. Inspection reports CONFIG_ONLY, never infers that
existing files prove memories are enabled. Config profiles and command overrides
must be checked in the actual client.

## Commands

```sh
qdrant-memory codex inspect --codex-home /canonical/test-codex-home
qdrant-memory --home /canonical/profile codex migrate --codex-home /canonical/test-codex-home --dry-run
# Review source hashes, skipped sections, scope and preview in the source file.
qdrant-memory --home /canonical/profile codex migrate --codex-home /canonical/test-codex-home --confirm --verify
qdrant-memory --home /canonical/profile codex migrate --codex-home /canonical/test-codex-home --confirm --resume --retry-failed --verify
qdrant-memory --home /canonical/profile codex export --output ./new-handoff --confirm
qdrant-memory codex verify --output ./new-handoff
```

Explicit output paths must stay within the invocation directory. Source paths
are explicitly authorized canonical Codex homes, with symlink/reparse ancestors
rejected. No native file, transcript, credential, DB or production collection is
modified. A source file is bounded to 2 MiB. Reads use one no-follow descriptor
and descriptor metadata, including the provenance mtime. Heading splitting honors
backtick/tilde code fences; unclosed fenced tails stay quarantined.
v1 imports semantic sections from
MEMORY.md (summary fallback); v2 imports summary sections. Duplicate normalized
sections are imported once, retaining both source mappings. Differently worded
summaries are not automatically declared equivalent. Semantic deduplication,
project selection and user-supplied sensitive data classification require review.
Code fences and likely credentials are skipped. This heuristic cannot identify
all personal data: inspect the source and dry run before approving any import.
Large sections exceeding Runtime payload or the CPU model's 128-token bound fail
explicitly; there is no silent truncation or cloud fallback.

Imports use the configured fixed user/agent scope, existing migration manifests,
SQLite operations, retries, fingerprint checks, WriterLease and crash recovery.
Content-derived IDs make unchanged snapshots repeatable. Changing content creates
a new logical source record; old records remain until an explicit authorized edit.
The generic Mem0 mapper remains unchanged for ordinary JSON imports.

Use repeated `--heading` options to select exact source topic/category headings,
including project-named sections, before importing. Heading categories survive
export. Destination scope always comes from the fixed profile, never source text.

## Exit, backup and rollback

Each handoff file has a separate 64 MiB bound. The writer checks the final UTF-8
JSON, Markdown and manifest bytes before creating the output directory, and
verification uses that same bound. The small native-source limit does not apply
to a larger valid handoff. Verification parses the same JSON bytes it hashed.

Export selects exactly the profile user/agent scope. The JSON preserves source
metadata and timestamps; SHA-256 covers each text, the logical record list and
both JSON/Markdown files. An interrupted handoff directory is incomplete and must
fail verification; select a new directory to retry. Existing exports are never
overwritten. No raw credentials or source paths are included in migration reports.

Handoff is **HANDOFF_PENDING**, never NATIVE_VERIFIED. Review the Markdown or JSON
in a local Codex chat. Ask Codex to remember each approved fact using the installed
client's supported memory interaction, enable native memories there, and verify
the actual resulting memory in a fresh chat. This can use the client's model
processing path; obtain the user's approval before submitting private content.
Do not write generated Codex files or internal SQLite tables. AGENTS.md requires
separate authorization and is project instruction, not native memory import.
Retain Qdrant until adoption is independently confirmed. Disable/uninstall the
plugin using Codex; this never deletes Qdrant, native files or handoff bundles.

Protected Shared Owner collections remain protected. Offline operator migration
does not bypass enrollment or start a second writer. Stop competing runtimes only
with authorization; for concurrent agents use existing credential-bound Shared
Owner MCP. Shared Owner bulk migration is not implemented by this adapter.

## Provider migration

Reuse Runtime's existing `setup`, `prepare-model`, `doctor`, `migrate mem0` and
JSON export. CPU is the Codex onboarding default. Ollama and OpenAI-compatible
services are explicit alternatives, never inferred from Codex login credentials.
Changing provider/model/instructions/normalization changes the pipeline identity.
Export a backup, choose a new profile/collection, re-embed with resumable migration,
verify, then approve the client configuration switch. Keep the previous collection
for rollback. Same dimensions do not establish compatibility.
