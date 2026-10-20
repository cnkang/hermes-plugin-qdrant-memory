# Validation and compatibility

This page records reproducible checks and the revisions to which they apply.
For installation, see the [README](../README.md). For supported deployment
boundaries, see [security](security.md) and [standalone setup](standalone.md).
A passing result for one revision does not certify later changes, every agent
model, or a production deployment.

## Runtime 0.3.0 checks (2026-10-16)

Release-preparation checks on the preparation head
([PR #53](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/53)) passed:

- Local: 44 files, 442 tests passed, 0 failed, 4 skipped (behavior contract
  suite).
- [Behavior/security CI](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/38763388693),
  [Platform compatibility](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/38763388746),
  [Standalone acceptance](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/38763388459),
  [Optional CPU embedding](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/38763388706),
  [Runtime packaging](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/38763388870)
  and [CodeQL](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/38763388632).
- The optional CPU embedding job prepares the pinned model artifacts and runs
  the real offline model acceptance on Linux, macOS and Windows with Python
  3.11.

## Runtime 0.2.1 checks (2026-10-13)

Behavioral checks on revision `b7b42799f29ee3ba83b91aa4f7cec3b30cc4dd21` in
[PR #37](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/37) passed:

- Local Python 3.14.7: 355 tests with minimum Hermes v0.21.5
  (`f97608f178d1ffeca59860195ab7da295f7c8e5f`) and 355 with latest main
  (`63d0b94120027d487c9a5235b9ec64b696af1b26`), zero skips. Local vectors and
  LLM responses are deterministic fixtures, not live model certification.
- Fresh Python 3.11.15 wheel and Python 3.14.0 sdist: 30 standalone tests each,
  zero skips, real installed CLI/public API/official stdio MCP, no Hermes.
- [Behavior/security CI](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/38363304389):
  minimum/latest Hermes × Python 3.11/3.14, 357 passing tests and one secret-gated
  Cloud skip per lane. Disposable Docker Qdrant HTTP/gRPC Server contracts ran.
- [Three-platform contracts](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/38363172850),
  [standalone matrix](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/38363172820),
  [wheel/sdist packaging](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/38363304369)
  and [CodeQL](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/38363304404).

The new regressions first failed on all three no-LLM entrypoints before the
fix. Explicit writes retain distinct texts at cosine 0.92, 0.97 and 1.0. Exact
canonical duplicates skip only within scope. Automatic factual relations pass.
Schema/IDs/ledger/migration code remains unchanged. No personal data or paid LLM
was used. Local Docker was unavailable, and Cloud was not executed for these checks.
Later documentation commits require their own current CI checks. Public PyPI
0.2.1 installation and catalog upgrade acceptance run in separate release steps.

## Source-preview validation (2026-10-12)

The following sections are historical evidence for their stated revisions.
Runtime 0.2.0 subsequently published standalone CLI/MCP on PyPI. The 0.2.1
release adds explicit-write regressions in `tests/test_explicit_dedupe.py` and
`tests/standalone/test_explicit_writes.py`. Run the current CI and package
acceptance before certifying a revision. See [0.2.1 notes](releases/0.2.1.md).

Runtime revision `2f4150bd14fa004f84511ad534b189e088c694b8`, submitted in
[PR #35](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/35), includes
durable transcript-author isolation and the standalone CLI/MCP interfaces.
These interfaces are a source preview. They are not included in the published
v0.1.0 release or its Hermes catalog pin.

| Check | Verified boundary | Public result |
| --- | --- | --- |
| Backend CI | Minimum supported Hermes and latest upstream `main`, Python 3.11/3.14, behavior contracts, disposable Qdrant Server, Ruff, Snyk and SonarCloud | [CI run](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/38264152648) |
| Platform compatibility | Linux/macOS/Windows × Python 3.11/3.14, embedded storage and Hermes lifecycle | [Platform run](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/38264152705) |
| Standalone CLI/MCP | Installed JSON CLI, public API and official MCP protocol contracts without Hermes, across the same OS/Python matrix | [Standalone run](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/38264152730) |
| Code scanning | Python and GitHub Actions CodeQL | [CodeQL run](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/38264152715) |
| Companion skill | Package distribution, Hermes E2E, Pi loading, standalone CLI/MCP and fresh runtime installation | [Skills run](https://github.com/cnkang/skills/actions/runs/38264236539) |

The local backend suite passed 342 tests using an isolated profile and
deterministic embedding/LLM fixtures. Regression coverage includes mixed authors,
restarts, branch/reset transitions and failed author-record writes. A fresh
`npx skills@1.7.0` installation from GitHub at skills revision
`acbce68b925a8f735986d15b15fb701099533a16` installed this backend revision.
It verified package provenance. It passed CLI initialization/diagnostics/write
and official MCP discovery/search/delete.

These tests do not use personal memory collections or paid model calls. The
fresh installation test uses localhost embeddings. CLI/MCP scope sharing is
tested with sequential clients. It does not establish concurrent-writer support.
See the [MCP compatibility guide](mcp.md#evidence-and-limits) for agent-specific
registration, discovery and tool-call boundaries.

## Initial standalone snapshot (2026-10-12)

The initial standalone implementation in
[PR #33](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/33) merged as
`8fd42572738b15a1abcad62aeecd0de9b1877e3a`. The initially reviewed head of the
subsequent [documentation PR #34](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/34),
`efe1c943e01c9f22213908365a01d4a8711d47e2`, is its direct child. That update
changed only documentation and `CHANGELOG.md`: standalone runtime sources were
unchanged, while the complete trees differed.

That implementation passed 326 local backend tests and 27 standalone contracts
in a fresh Python 3.11 wheel installation. Recorded CI results include
[backend CI](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/38225834759),
[standalone compatibility](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/38225834720),
[platform compatibility](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/38225834663)
and [CodeQL](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/38225834614).
Local acceptance used Codex CLI 0.160.0, OpenCode 2.0.24 and Pi 1.1.0 with
disposable profiles. These results establish the documented interface boundaries,
not unrestricted live-model compatibility.

## Hermes catalog installation (2026-10-11)

The official Hermes Plugin Catalog pin
`5ec9cac094e5e9dc80e1cd25b241bcec44019d5b` was tested on a fresh profile with
embedded Qdrant and local Ollama. Discovery, installation by catalog name,
dependency preparation, provider registration, initialization, real memory
writes, cross-session recall, restart recovery, update/delete, inventory/export
and scoped deletion passed. See the
[catalog acceptance record](validation/catalog-integration-acceptance.md) for
versions, provider limitations and reproduction steps.

That record was merged in [PR #31](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/31)
at `1f58b8534df7eb94d2385bfceb6927b850545031`, with passing
[CI](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/38112568943),
[platform compatibility](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/38112568962)
and [authenticated Cloud smoke](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/38112568954).
Catalog installation uses its own immutable pin. Newer default-branch
documentation and source-preview features do not change that pinned version.

## Published release and live acceptance

The [v0.1.0 release notes](releases/v0.1.0.md) describe the Hermes technical
preview and its support limits. Recorded release validation includes
[CI](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37896752887),
[six OS/Python jobs](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37896752957)
and [authenticated Cloud integration](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37896752899)
at `112e403c414f90905325322bab135493a0b03b85`. Post-release scoped-deletion
validation also passed
[CI](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37943322913),
[platform compatibility](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37943322889)
and [Cloud integration](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37943322869).

A live Quick Start on 2026-10-09 used a fresh macOS arm64 profile and local Ollama
embeddings. It passed installation, conversation extraction, cross-session
recall, update/delete and restart verification. Slow local chat models exceeded
the default auxiliary-call timeout. The [troubleshooting guide](troubleshooting.md)
explains timeout configuration and retry. This was one deployment, not a
production capacity benchmark.

## Reproducing checks and interpreting results

Use [development setup](development.md) for the host runner, isolated dependency
environments, lint and local tests, and [CI services](ci.md) for required matrices
and scanner configuration. Each latest-Hermes job records its actual upstream
SHA because upstream `main` can change between runs.

Authenticated Cloud smoke is optional and explicitly skips when credentials are
absent. Such a skip does not validate Cloud behavior. Test execution, not
configuration alone, establishes the reported boundary.

Retrieval and recovery measurements are separate synthetic checks. See the
[retrieval baseline](retrieval-baseline.json) and
[recovery benchmark](recovery-benchmark.md) for fixture sizes, measured revisions
and limitations. They do not establish production retrieval quality or capacity.
