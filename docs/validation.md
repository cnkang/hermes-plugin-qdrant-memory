# Validation evidence

## Post-merge main snapshot (2026-10-09, PRs #15–#18)

PRs #15–[#17](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/17)
are merged on top of the snapshots below. The earlier
`ff5712d318f48054da3443598f58f6793d610514` snapshot passed:

| Gate | Evidence |
| --- | --- |
| CI, including immutable host matrix, Server tests and security scans | [37887162232](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37887162232) |
| Linux/macOS/Windows × Python 3.11/3.14 | [37887162226](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37887162226) |
| Authenticated Cloud integration | [37887162253](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37887162253) |

PR #17 delivered the memory inventory/export/scoped-deletion commands
(issue #15); PR #16 delivered the release/community readiness set. The
**current certified revision** is `112e403c414f90905325322bab135493a0b03b85`
(2026-10-09): the scoped-deletion
hardening in the follow-up finalization PR (#18) is validated on its own head
and re-certified by the gates on main at release time, with
[CI](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37896752887),
[six platform jobs](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37896752957)
and [authenticated Cloud integration](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37896752899)
all green; the `v0.1.0` tag will be created from the documentation commit that
records this certification, with no functional changes since `112e403c`. The
snapshots below retain their original scope.

The project is a limited technical preview. The `v0.1.0` release will be created
from this certification record.

## Post-merge main snapshot (2026-10-09)

PRs #13 and [#14](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/14)
are merged on top of the snapshot below. Main
`f6a2e97f9cd16c4aed3ba2590bfcf36176ee5e29` passed:

| Gate | Evidence |
| --- | --- |
| CI, including immutable host matrix, Server tests and security scans | [37868895051](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37868895051) |
| Linux/macOS/Windows × Python 3.11/3.14 | [37868895110](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37868895110) |
| Authenticated Cloud integration | [37868895002](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37868895002) |

PR #14 makes verify mode report `migration_incomplete` ahead of superseded
conflicts; PR #13 synchronized this documentation with the merged state. The
snapshot below (2026-10-08) retains its original scope.

## Post-merge main snapshot (2026-10-08)

PRs #10, #11 and [#12](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/12)
are merged. Main `32bd4e7abdb3b417cc5b0dd0f793f93db188db3b` passed:

| Gate | Evidence |
| --- | --- |
| CI, including immutable host matrix, Server tests and security scans | [37802338914](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37802338914) |
| Linux/macOS/Windows × Python 3.11/3.14 | [37802339623](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37802339623) |
| Authenticated Cloud integration | [37802338838](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37802338838) |

The Cloud job's `Exercise authenticated Cloud` step executed successfully; this was
not a missing-credentials skip. The earlier candidate branch rejection is historical.
The main-only environment restriction remains intact. These results certify the
named code snapshot, not later documentation commits or future Hermes revisions.

The project is a limited technical preview. A live Quick Start
drill on 2026-10-09 exercised the full user flow on a fresh home (see the drill
record below). Synthetic retrieval and recovery measurements still do not
establish production quality or capacity. See the
[final pre-release review](releases/PRE_RELEASE_FINAL_REVIEW.md) for historical local
results, exact upstream Hermes revisions and support limits, and the
[recovery benchmark](recovery-benchmark.md) for the separate storage/replay evidence.

The sections below retain earlier evidence with their original scope.

## Live Quick Start drill (2026-10-09)

A fresh isolated home (custom `HERMES_HOME`, no development shims) installed the
provider from the public repository and ran against local Ollama on a macOS arm64
host (24 GB). Flow: install → configuration → conversations → automatic
extraction → cross-session recall → update → delete → restart.

| Step | Result |
| --- | --- |
| Fresh home, clone, `hermes memory setup`, `hermes qdrant-memory init` | PASS — dependencies prepared through Hermes PM; embedded collection created; embedding probe OK (`qwen3-embedding:4b`, 2560 dimensions) |
| Conversation writes via the agent tools | PASS — add/search/update/delete all committed in the ledger |
| Automatic turn extraction | PASS — the pending backlog drained: events and operations committed, dedupe recorded ADD and SKIP decisions |
| Cross-session recall | PASS — new sessions returned the taught facts from stored memory |
| Update | PASS — the codename entries were rewritten with the new value and the old one marked obsolete |
| Delete | PASS — the targeted memory was removed, stayed gone on re-check, and the committed event bodies were scrubbed to empty payloads |
| Restart and consistency | PASS — `verify` reports `ok: true` (exact count, zero invalid IDs); sessions after restarts recalled the surviving facts |

Drill findings and response:

- **Fixed in this release**: provider discovery imported `portalocker` at module
  load, so a home whose dependencies were not yet prepared reported “provider
  not found” during `hermes memory setup`. The import is now lazy and covered by
  a regression test.
- **Documented**: extraction and relation checks run as Hermes auxiliary calls
  (30 s per-task default). Slow local models should raise
  `auxiliary.qdrant_memory_extraction.timeout` and requeue timed-out work with
  `hermes qdrant-memory retry`.
- **Environment notes**: a 27B local chat model plus local embeddings exceeds
  comfortable VRAM on a 24 GB host (Ollama evicted runners under concurrent
  load); the interaction phase finished on a hosted model through the same local
  Ollama daemon. Local-model turns took 8–11 minutes each; hosted-model turns
  took seconds. Extraction produced near-duplicate variants below the dedupe
  review threshold — consistent with the conservative similarity design; tidying
  them is part of the deferred memory inventory/export work.

The deterministic CI gates above remain the release's hard evidence; this drill
evidences installability and the user-facing flows on one real machine.

## Recorded rereview snapshot (2026-10-06)

This section records the earlier snapshot only. PRs #10 and #11 subsequently
merged. Main `79565e755716f3d812b73e550417b5049b9642e6` passed
[CI](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37788043978),
[all six platform jobs](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37788044014),
and [authenticated Cloud integration](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37788044033).
The following unavailable-service statements apply to the historical local run.

Minimum supported host release: v0.21.5
(`f97608f178d1ffeca59860195ab7da295f7c8e5f` = v2026.9.24). Earlier releases were
probed empirically on 2026-10-09: v0.21.0–v0.21.2 cannot import required host APIs
(24 test modules fail collection); v0.21.3 fails host-lifecycle contracts; v0.21.4
lacks profile-scoped secret resolution (`set_secret_scope(profile_home=...)`).
Local full suites use the minimum tag's native runner on Python 3.11 and pinned
`4787e4d56fc8d9265d4c7d3c0fe5accee86b4078` on Python 3.14: 19 files,
104 passed, one optional Cloud skip. The required CI matrix validates the minimum
release and the latest Hermes `main` on both Python versions; each latest-main run
records its checked-out SHA, so a green run applies to that exact SHA.

Real native setup, checkpoint v2 normalization/compression and session-manager
paths are exercised. The weekly/manual tracker also requires a host lifecycle test
for the `MemoryManager.sync_all()` to provider admission path, with
`HERMES_QDRANT_REQUIRE_HOST_SYNC=1` so missing host APIs fail instead of silently
skipping. Tracker runs record their checked-out SHA in the run summary; existing
evidence does not
establish that a turn waiting in Hermes's in-memory queue survives a host crash or
bounded shutdown before `sync_turn` runs. The plugin durability boundary starts at
its own ledger admission. Setup's dependency installer is stubbed; dependency install
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
