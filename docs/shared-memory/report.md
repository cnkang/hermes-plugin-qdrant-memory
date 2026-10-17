# Cross-agent shared memory delivery report

Date: 2026-10-17 (Asia/Shanghai). Feature status: **unreleased, opt-in local MVP**.
Hermes compatibility is the first release gate. No package/tag/Release/Registry
publication, automatic profile reconfiguration or production-data migration was
performed. All test records/collections were synthetic and disposable.

## 1. Repository baseline

All began clean on `main` and matched upstream HEAD checked during this task.
Implementation branches: `codex/shared-memory-service` in all three repositories.

| Repository | Initial HEAD | Existing version |
|---|---|---|
| hermes-plugin-qdrant-memory | c24ac4f0fd0d67be5aadfc3d6719f31339820089 | 0.3.0 |
| qdrant-memory-mcp | 36ff391bf835eb4adeeac9f0bd759a97403de59a | 0.1.4 |
| skills | c2d755cd6df3f9af069d9faff10fbcfa5b29806e | qdrant-memory skill 0.2.6 |

The Runtime baseline described as `v0.3.0-4-gc24ac4f`; MCP as
`dsh-bundle-v0.1.0`; skills as `skills-20261016.2-2-gc2d755c`. Declared package
versions/pins remain unchanged because this is a reviewed source preview.

## 2–4. Assessment, alternatives and decision

The detailed [ADR](architecture.md) records code evidence and a correctness,
recovery, security, performance, complexity, operations, compatibility,
deployment and maintainability comparison of all three options.

- **A, direct clients:** concurrent Server/Cloud readers are technically feasible
  without a writable ledger, but need a separate validated read-only access path.
  Existing standalone readers still take WriterLease; independent embedded opens
  are unsupported. Independent direct writers do not share Ledger coordination.
- **B, single owner — selected and implemented:** one process owns the existing
  Runtime, Qdrant, embedding, SQLite and WriterLease. Authenticated clients share
  explicit spaces through a private local API and thin stdio MCP bridges.
- **C, distributed multi-writer — deferred:** Qdrant ordering does not provide an
  application-level atomic scope/version check plus write. Distributed fencing,
  reconciliation and conflict handling are not implemented or claimed.

Existing deterministic point-ID/hash, operation keys, SQLite WAL/FULL, `wait=True`,
embedding fingerprints, delete fences, scope filters and recovery semantics are
retained. WriterLease is a **profile-local OS lock**, not cross-profile or
cross-host fencing. Operators must keep exactly one canonical owner per target.

## 5. Implemented functionality and changed files

| Runtime file | Responsibility |
|---|---|
| `qdrant_memory/shared_service.py` | Fixed credential grants, strict schemas, scope/marker authorization, CAS revisions, durable request plans/receipts, bounded authenticated loopback API and audit decisions |
| `qdrant_memory/shared_client.py` | Loopback-only client, sanitized errors, official SDK stdio bridge; no local storage/embedding |
| `qdrant_memory/shared_state.py` | Read-only pending-plan interlock, without creating schema in legacy profiles |
| `qdrant_memory/standalone.py` | New explicit `shared-serve`, `shared-call`, `shared-mcp`; default private mode preserved |
| `qdrant_memory/mcp_server.py` | Injectable catalogue/dispatcher for the opt-in bridge; existing default schemas preserved |
| `qdrant_memory/cli.py` | Refuse mutating reset/delete/import around unfinished shared plans; operator retry remains available |
| `tests/standalone/test_shared_service.py` | Independent process clients, true embedded/Server storage, crash points, isolation, concurrency, readonly, retries and audit secrecy |
| `tests/standalone/test_shared_evaluation.py` | Metric correctness for rank/miss penalties |
| `tests/test_checkpoint_lifecycle.py` | Compatible exception import for both current and minimum Hermes; no weakened checkpoint assertions |
| `scripts/evaluate_shared.py` | Reproducible real-embedding private/shared quality, latency, throughput, footprint and restart/recovery measurements |

The Native Provider, extraction engine, models/ID algorithm, QdrantStore, Ledger,
WriterLease, normal tool schemas and managed provisioning code are unchanged.
The only existing maintenance change is a fail-closed interlock for profiles
that explicitly used the new feature and still have unfinished shared plans.

Companion MCP changes: separate preview JSON, DSH Cordis and Codex TOML configs,
English/Chinese README/CHANGELOG and inclusion of examples in the sdist manifest.
No runtime engine is copied and no published dependency pin is redirected.
Skill changes: canonical shared-memory reference, schemas/retry instructions,
private/readonly boundaries and explicit Native Provider integration limits.

## 6. Supported versus deferred scenarios

**Implemented and verified:** agent A saves, B in a different process/session
reads/updates, A sees the committed revision; explicit private spaces and legacy
records stay isolated; a read-only credential cannot mutate; stale concurrent
updates conflict; reconnect/restart returns a durable receipt without duplicate
logical insertion; embedded and Server backends both work under one owner.
Official MCP SDK stdio sessions with different Hermes-fallback/DSH principal
credentials verify the protocol-level cross-agent scenario.

**Implemented but not fully host-validated:** manual shared MCP configs for DSH,
WorkBuddy and Codex; Hermes MCP fallback in a profile without the Native Provider
interface. Actual application UI/bundle import acceptance is not claimed by SDK
process tests. Existing managed private configurations remain unchanged.

**Designed, not implemented:** Native Provider automatic shared extraction,
per-turn human/space consent in the native lifecycle, direct read-only Qdrant
clients, remote-agent OAuth/API, grant management UI, receipt retention compaction.

**Rejected/deferred:** independent embedded processes, automatic legacy sharing,
automatic collection/model migration, public endpoints and distributed multi-writer.

## 7. Identity and authorization

A private operator policy maps a high-entropy token's SHA256 to one principal,
stable owner, space, visibility and read/write permission. User/agent/space or
vectors in MCP arguments are rejected. A token selects one preconfigured space;
no tool argument can widen it. `user_id` remains the owner; reserved
`agent_id = shared:v1:<space>` is a stable partition, not the creating agent.
`created_by`/`updated_by` preserve provenance. Private spaces cannot grant other
principals; shared/project spaces permit explicit multiple grants.

Shared payloads add an explicit protocol marker, space, visibility and opaque
UUID revision. Queries require marker plus exact scope/visibility; ID operations
validate the same boundary before checking versions. Legacy records, including
legacy null-agent or reserved-scope records, remain private. Audit stderr includes
hashed principal and allow/deny only. The endpoint rejects unknown credentials,
Origin headers, invalid Host, transfer encoding, oversize requests and non-JSON
requests. Clients reject remote URLs, redirects and environment proxies.

## 8. Concurrency and recovery

All reads/writes use the Runtime RLock. Update/delete requires the exact revision
read previously. New revisions survive recovery; delete/recreate uses a fresh
revision to avoid ABA. A stale write returns explicit `SharedConflictError`.

Before any Qdrant write, a complete immutable plan/response is durably persisted
in an additive destination-namespaced SQLite request table. Runtime then prepares
and commits the operation with the original retry/ack path. Restart recovers
pending operations and plans before readiness. Completed plans are scrubbed;
request hashes/receipts remain to reconcile ambiguous retries. Reusing an ID for
different input conflicts. A historical receipt does not assert current state.

After uncertain failure, new writes stop. Terminal Runtime failures require
operator retry, followed by owner reconciliation. A disconnected client never
cancels durable admission. Unfinished plans block normal private tool admission
and destructive/import maintenance so delayed replay cannot resurrect deleted
records. After reconciliation, legacy deletion/export/migration remain usable.
Portable Mem0 export is not a full shared backup: grants/revisions/receipts need
a coherent stopped-owner profile plus Qdrant backup. See the operations guide.

These guarantees require no external direct writers. The local lease cannot
prevent another profile/host from bypassing the service with Qdrant credentials.
Same-OS-user hostile code can read environment/storage and is outside the bearer
credential boundary. Windows private file ACLs remain operator-managed.

## 9. Automated acceptance evidence

| Gate | Observed result |
|---|---|
| Actual Hermes current main `0ab43d4f67e41be75f4f6c0fa14a86dbcfd3e5b3`, canonical runner, Python 3.14.8 / SDK 2.3.0 | **477 passed, 0 failed, 4 skipped**, 172.5 seconds |
| Declared minimum Hermes `f97608f178d1ffeca59860195ab7da295f7c8e5f` (v0.21.5), same canonical runner/environment | **477 passed, 0 failed, 4 skipped**, 173.1 seconds |
| SDK 2.x standalone subset inside each full host run | **70 passed**, including both embedded and Server, no skips |
| Fresh built wheel, Python 3.12.15 / SDK 1.30.0, isolated import mode | **68 passed**, 111.23 seconds; the subsequently added audit cases separately **2 passed**, and expanded outage/restart cases **2 passed** |
| MCP metadata/protocol/managed integration suite | **140 passed**; after adding preview examples to the sdist manifest, artifact/release guards **8 passed**, rebuilt artifacts and strict Twine checks passed |
| Skill targeted distribution/Hermes notice/package checks | **58 passed, 6 skipped** (opt-in host/CLI fixtures and Windows-only behavior) |
| Repository-pinned Ruff 0.15.1, formatting and staged pre-commit snapshots | **Passed** |
| Git whitespace/diff checks, private default schemas and native core comparison | **Passed**; Native Provider/Runtime/Ledger/WriterLease/ID/store/extractor/tool sources match the initial baseline |

The four optional host skips are three prepared FastEmbed CPU-cache cases and
one live Cloud case. They are not treated as passes. Real Ollama Qwen inference
was exercised separately in both benchmark backends. Current/minimum host test
logs are retained locally at `/tmp/qdrant-shared-hermes-current-final.log` and
`/tmp/qdrant-shared-hermes-minimum-final.log`; the tables record their final
observed summaries, not the runner's approximate discovery count.

Recorded commands (substitute absolute checkout/environment paths):

```sh
# Canonical Hermes runner, serial because remote cases restart the test container.
HERMES_PYTHON=/absolute/path/.test-env/bin/python scripts/run_tests.sh \
  /absolute/path/plugin/tests -j 1 --file-retries 0 -- --tb=short \
  --qdrant-test-url=http://127.0.0.1:16333 \
  --qdrant-test-container=qdrant-shared-memory-acceptance

# Fresh installed wheel; no editable/source import, SDK 1.x.
.sdk1-env/bin/python -I -m pytest --import-mode=importlib -q tests/standalone \
  --qdrant-test-url=http://127.0.0.1:16333

.lint-env/bin/ruff check .
.lint-env/bin/ruff format --check .
```

Initial environment/setup failures were resolved before final acceptance:
missing build artifacts in distribution tests, incomplete host sparse modules,
SDK result alias access in a new test, and the upstream checkpoint exception
relocation. No Native Provider behavior was changed to hide those failures.
No live Cloud account or optional downloaded FastEmbed CPU model was exercised.
Hosted SonarCloud/Snyk and the complete OS/Python CI matrix still require CI.

Pass/fail criteria: zero failed final acceptance cases; zero unauthorized
search/ID mutations; all read-only writes denied; exactly one winner per stale
revision race; identical IDs/revisions after crash replay; second same-profile
owner rejected; existing Hermes native lifecycle and standalone contracts pass.
Host UI/remote multi-writer claims remain disabled until independently accepted.

## 10. Benchmarks and quality

| Backend | Mode | Search p50 / p95 ms | Durable writes/sec | Repeated search/sec (1 reader) | Repeated search/sec (4 readers) |
|---|---|---:|---:|---:|---:|
| embedded | private | 15.27 / 19.51 | 43.93 | 55.57 | 61.50 |
| embedded | shared | 29.68 / 43.77 | 28.04 | 40.61 | 50.22 |
| server | private | 20.36 / 27.62 | 23.56 | 57.75 | 51.82 |
| server | shared | 36.39 / 49.33 | 19.96 | 30.97 | 39.91 |

Ready-owner RSS was 106016 KiB (embedded) and 107360 KiB (Server).
Empty-backlog restart readiness was 496.83 / 485.06 ms; restart with one durable
pending plan was 499.28 / 514.49 ms (embedded / Server).

Raw evidence: [embedded](benchmark-embedded.json), [Server](benchmark-server.json).
Reproduce with `python scripts/evaluate_shared.py` and then
`python scripts/evaluate_shared.py --qdrant-url http://127.0.0.1:16333`.
All stores are temporary; remote collections have random generated names and
only those generated collections are deleted. No production profile is loaded.

Dataset: 21 synthetic memories, 24 labeled queries. SHA256:
`f2f393c30b3007bf16eb0ef1e7e6c9ace23c311f119e91f59dbc3c71d3210a51`.
Ollama `qwen3-embedding:0.6b`, 1024 dimensions, model digest
`ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d`.
Qdrant Server 1.17.1, client 1.17.1; Python 3.14.8 on macOS arm64.
Embedding fingerprint is recorded in both JSON artifacts.

Both modes/backends achieved Recall@5 **1.0000**, Precision@5 **0.2333**,
MRR **0.9479**, nDCG@5 **0.9595**; shared cross-user leakage count **0**.
Labels contain few relevant items per query, so Precision@5 is not a production
quality statement. Separate authorized credentials are used for shared writes
and retrieval. No quality improvement is claimed.

A private open Runtime is compared with a shared call including a fresh local
HTTP client. Shared service overhead is visible, not removed from the results.
Four-reader latency includes queueing and embedding contention. Small samples,
cache/warmup effects and other local workloads preclude throughput improvement
claims. RSS is ready owner process only, excluding Ollama/Qdrant and OS caches.
Readiness/recovery timing includes Python startup, pipeline/schema validation,
and, where labeled, exactly one durable synthetic pending request. That pending
measurement uses a controlled after-plan interruption; independent process-death
correctness is tested separately. Large-backlog live recovery was not benchmarked.

## 11. Documentation and product positioning

English and Chinese Runtime READMEs now link the shared preview. The new
[English operations guide](README.md), [中文指南](README.zh-CN.md) and ADR cover
private/shared/read-only/project spaces, installation/configuration, fixed
identity, single-owner coordination, embeddings, supervision, backup/recovery,
upgrade/rollback, multi-host limits, error handling and the explicit
Hermes-fallback → DSH walkthrough. Existing DSH/WorkBuddy defaults are unchanged.

The [official Qdrant MCP](https://github.com/qdrant/mcp-server-qdrant) provides
semantic storage/retrieval, persistent Qdrant backends and metadata filtering.
Qdrant Memory adds agent lifecycle/recovery and this controlled opt-in sharing
protocol. No unsupported claims about official persistence/isolation/reliability
are made. The local service is not advertised as Streamable HTTP MCP; agent-facing
MCP stays stdio, following the [MCP transport contract](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).

## 12–13. Follow-ups and suggested release sequencing

1. Review the ADR/security boundary and preserve the Hermes minimum/current
   compatibility gates as blocking. Finish hosted checks and platform matrix.
2. Candidate Runtime **0.4.0**, MCP **0.2.0**, skill **0.3.0** for the opt-in
   shared service after review. These are suggestions, not new tags/versions.
   The shared-only revision/retry schema is an explicit additive mode.
3. Publish Runtime only through normal release procedures, then update the
   metadata distribution/managed immutable artifact pins to the actual released
   version, never to an unpublished moving branch. Run DSH/WorkBuddy/Codex UI and
   bundle import acceptance before labeling managed sharing available.
4. Add a native Hermes explicit-space/author consent contract without changing
   existing private automatic extraction. Until then native automatic shared
   extraction stays unavailable; never register duplicate memory interfaces.
5. Design receipt retention, richer authorization/remote OAuth and lifecycle
   supervision separately. Distributed multi-writer needs its own proven
   coordination/recovery design and is not a promised next release.

No existing collection needs a migration for the MVP. Any future legacy sharing
migration must include preview, coherent backup, explicit consent, verification
and rollback; it must not be implicit on service startup.

## 14. Commits

| Repository | SHA | Message |
|---|---|---|
| Runtime | `6e4acb4ef288ef2225a7bb76205a0dc58f7b026f` | `test(hermes): support relocated checkpoint exception` |
| Runtime | `d4703af1ea7825c912772b0592839580af7eb4ba` | `feat(shared): add authenticated single-owner memory service` |
| Runtime | `c3fa1e7d5472cf39346772dfd05e0442d54780c9` | `test(shared): add reproducible retrieval and latency evaluation` |
| MCP | `147f94ee26da422e5bdcb8c49a3e8f50f8a67ab4` | `docs: add opt-in shared memory host integration previews` |
| Skills | `407959ba14a3156a7fa6358bae9ef20ac8bc00bb` | `docs: define shared owner permissions and retry workflow` |

This report, operation guides and benchmark JSON are the separate final
`docs(shared): document architecture and verified acceptance` batch. Its containing
commit SHA is recorded in the chat delivery receipt and branch history (a file
cannot embed the hash of its own containing commit). All batches use the
`codex/shared-memory-service` branch; package versions/tags remain untouched.
