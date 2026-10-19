# Shared destination hardening validation — 2026-10-19

## Review follow-up

Claim publication now writes and fsyncs a private same-directory temporary file
before atomic replacement and directory synchronization. Interrupted serialization,
sync and publication tests confirm that readers never observe partial final
claims and that enrollment can safely be retried. Registry creation classifies
EACCES, EPERM and EROFS consistently: legacy fallback is allowed only without a
protected policy or an existing registry; other errors remain fail-closed.

The authenticated CI fixture is pinned to its verified image digest, shared by
the pull and test commands. Explicit shared CLI policy paths now use the existing
invocation-directory boundary before file access; services must start from the
Owner directory or a deliberately chosen enclosing directory. The deployment
guide documents this compatibility restriction. Programmatic policy loading
retains its trusted-operator contract.

The added read-only doctor tests exercise real Qdrant schema/identity reads,
incompatible/missing identities, non-mutating results and occupied versus idle
local ownership. Small helpers reduce validation/recovery complexity without
changing recovery order or loosening quality thresholds.

Final local follow-up verification: 567 tests passed, 7 optional environment
tests skipped, with one existing embedded-index warning. Aggregate Runtime line
coverage is 92%; shared security is 90% and writer ownership 94%. The authenticated
TLS/JWT gate passed with the pinned image digest and matching client version.
Ruff and actionlint passed. Remote quality-gate closure requires the final pushed
revision's analysis; local coverage does not substitute for that result.

Transient action-download retries are documented separately in [CI operations](../ci.md).
Their 14 tests are included in the full result above. The classifier also matched
the observed macOS bootstrap DNS failure using read-only historical run metadata
and logs. Automatic triggering becomes available only after the controller is
merged into the default branch; test and quality-gate failures are never retried
by that controller.

## Repository baseline and versions

All three initial worktrees were clean. Existing branches were preserved and new
`codex/shared-destination-security` branches were based on fetched main branches.

| Repository | Initial local branch / HEAD | Fetched implementation baseline |
|---|---|---|
| Runtime | main / `1f6db227aa09f8ebd71618c6ec95ec4d7e138077` | `352fed94a3c7c4b261ded43975227265a7ce6309` |
| MCP | codex/defensive-hardening / `2e4da5526a454cacea5e34f7272a80446c4d243f` | `d708ca570980fbd3dba09d4b08679d9d8bc9a2a5` |
| Skills | codex/defensive-hardening / `4276afd5151fb335d4bbe53ab0be76d77e11635a` | `a3524b8ad3a2d66fb478e275531d929d1a345dc2` |

Verified public baselines: Runtime/tag 0.4.0, MCP/tag 0.2.1, DSH bundle 0.1.2,
DSH client/host fixtures 0.2.1-rc.2, managed Launcher 0.1.0-rc.3 and canonical
Skill 0.3.0 (snapshot skills-20261018.1). WorkBuddy connector 0.2.0 uses the same
Launcher and pins canonical Skill 0.3.0. Metadata, lockfiles, immutable Runtime/
Skill source pins and published artifacts remain unchanged. No release was created.

## Threat boundaries and implementation

The [versioned threat model](threat-model-v1.md) covers application grants,
OS/process ownership, database authorization, network restrictions and operational
guarantees, including remote/admin/same-account bypasses.

Existing controls retained: profile WriterLease, immutable durable plans,
idempotent receipts, recovery interlocks, read-only/fixed-scope grants, stale
revision rejection, private legacy record isolation, collection fingerprint and
vector validation, loopback transport limits and drain-before-lease-release.

New implementation commit: `005c8d7f69a10d445da3f1fa26ca6e2b27114298`.

| Files | Change |
|---|---|
| qdrant_memory/shared_security.py | Strict optional destination binding, private state/claim checks, account registry and non-mutating doctor |
| qdrant_memory/ownership.py | Shared account locks for ordinary writers and exclusive protected Owner locks; durable claims reject conflicting profiles |
| qdrant_memory/shared_service.py | Compatible optional policy envelope; protected umask and per-write state validation |
| qdrant_memory/standalone.py | Protected admission before recovery, expected schema check and shared-doctor command |
| tests/security/ | Negative binding, privacy, conflicting writer, replay/recovery, schema, redaction and database authorization contracts |
| scripts/test_shared_security_docker.py | Disposable loopback TLS/JWT fixture with separate bootstrap and Owner credentials |
| .github/workflows/shared-security.yml | Dedicated deterministic and real authenticated database isolation CI |

The existing backend namespace, payload schema, Ledger schema and shared request
protocol are unchanged. Policy protection is explicit opt-in. Once enrolled,
ordinary private, provider and maintenance writers to that destination fail
closed. POSIX ownership checks are required for protected mode; Windows existing
private/shared workflows retain their original mode. Private containers without
a passwd record or a writable account home and no existing registry retain
legacy lease behavior; protection requires offline account/registry provisioning.

Diagnostics use stable codes and PASS/FAIL/UNVERIFIED (exit 0/1/2). See the
[full command and deployment contract](deployment-security.md). Configuration,
state exposure, binding/schema mismatch and reliable local ownership conflicts
are rejected without automatic repair, deletion or collection creation. Recovery
finishes before readiness; write-state exposure after startup blocks admission.
Doctor never recovers and does not open embedded storage. Active WAL/readiness,
effective key scope, exclusive credential distribution and backup coherence are
not inferred as PASS.

## Qdrant authorization evidence

Official Qdrant security/access-control/Cloud-key documents were checked on
2026-10-19 and are linked in the deployment guide. Collection-granular JWT `rw`
credentials are distinct from administrative/signing keys; a global read-only
key is distinct from a collection-scoped key. TLS and network isolation are
separate controls. Self-hosted JWT RBAC requires explicit configuration.

The real fixture demonstrated that a collection-only key cannot create a
collection: Qdrant returned 403 (global access required). Provisioning and cleanup
therefore use a separate administrative test client; the running Owner receives
only its collection key. With that separation, direct Owner writes and an
authenticated shared-client write succeeded, while absent, read-only and
unrelated-collection credentials received 401/403. Read-only reads succeeded.
Remote doctor schema inspection passed while key isolation correctly remained
UNVERIFIED. All data was confined to a newly created disposable fixture collection.

## Verification evidence

Environment: Darwin 27.0.0 / arm64, Python 3.14.7 for Runtime, Python 3.11.15 for
MCP distribution, Node 22.23.2, Ruff 0.15.1. Runtime default test environment used
qdrant-client 1.19.1, httpx 0.28.1, portalocker 3.2.0 and MCP SDK 1.30.0.
The compatible database fixture and second SDK run used qdrant-client 1.15.1
with Qdrant 1.15.5 and MCP SDK 2.3.0. Fixture image digest:
`sha256:0fb8897412abc81d1c0430a899b9a81eb8328aa634e7242d1bc804c1fe8fe863`.
Hermes checkout: `8a7a5a905fc66ed82b922587a8e3c011e819dbb5`.

Commands below are repository-relative; `HERMES_CHECKOUT` denotes the tested host
checkout, and each Python is selected from its isolated test environment.

| Check / command | Observed result |
|---|---|
| Runtime: `PYTHONPATH="$HERMES_CHECKOUT" python -m pytest tests -q --tb=short` | 521 passed, 7 optional live-environment skips before final account-lookup compatibility test was added |
| Final ownership regression: `PYTHONPATH="$HERMES_CHECKOUT" python -m pytest tests/test_writer_ownership.py tests/test_backend_contracts.py tests/security/test_shared_security.py -q --tb=short` | 29 passed, including all 18 final deterministic protection cases |
| SDK 2: `python -m pytest tests/security/test_shared_security.py tests/standalone -q --tb=short` | 110 passed, no skips |
| Authenticated database: `python scripts/test_shared_security_docker.py qdrant/qdrant:v1.15.5` | 1 passed, no warnings with matching client; includes real Owner/client writes, denied writes and read-only doctor |
| MCP: `python -m pytest tests -q --tb=short` | 144 passed |
| DSH: `DSH_TEST_MODULES=... QDRANT_TEST_SKILL_ROOT=... python -m pytest tests/dsh_acceptance.py -q --tb=short` | 3 passed, official client persistence and CLI/bundle lifecycle |
| Launcher: `node --test tests/test_managed_bootstrap.mjs tests/test_managed_launcher.mjs` | 39 passed |
| Skills: `python scripts/validate_skills.py` | Passed |
| Skills package/test suite per validate workflow | 275 passed, 34 explicit optional host/runtime/installed-agent skips |
| Skills current Runtime/shared contract | 26 passed with opt-in variables enabled |
| Skills Hermes paths/runtime E2E | 5 passed with opt-in variables enabled |
| Runtime/MCP: `python -m build`; `python -m twine check --strict ...` | Wheel and sdist built; strict metadata checks passed |
| MCP: `python scripts/verify_metadata.py` | Metadata, pins, ownership marker, schema and metadata-only artifacts passed |
| Runtime: `python scripts/check_host_pins.py --host "$HERMES_CHECKOUT"` | Both shared host pins compatible |
| Ruff check / format and Git whitespace checks | Passed |

The full Runtime suite retained one existing embedded payload-index warning.
An initial run without Hermes import configuration failed collection, then passed
with the actual host. Two existing lease-injection tests required preserving the
default no-keyword call path. Skill exact-ID denial tests were corrected to send
valid delete arguments so they test scope authorization, not input rejection.
MCP metadata tests initially required local wheel/sdist preparation; the built
artifacts then passed all 144 tests. Skill packaging requires reviewed files to
be staged; with the new reference staged, its complete package suite passed.
An initial newer-client/older-server fixture emitted a compatibility warning;
the matching 1.15.1/1.15.5 run passed without suppressing warnings.

Cloud credential isolation, Cloud snapshots/network controls, other OS matrices,
and authenticated hosted SAST/dependency scans remain separate deployment/CI
evidence. No real Cloud credentials or production collections were used. Ordinary
CI reports the external isolation gate as UNVERIFIED when not opted in; the new
Docker job verifies a real self-hosted fixture without Cloud credentials.

## Release recommendation and sequence

Recommend **Runtime 0.4.1**: additive opt-in protection and diagnostics preserve
existing protocol, identifiers, data semantics and private defaults. Document the
intentional rejection of ordinary writers after enrollment and the requirement
to stop older writers before enabling protection. A new cross-container transport
would instead require **0.5.0** and is not implemented.

Review and merge Runtime implementation/docs first, require CI/security checks,
then deliberately prepare and validate its release. Only after Runtime is
published should a separate MCP release update its exact dependency/source pins,
Launcher/hash locks and acceptance results. Update Skill compatibility guidance
and WorkBuddy/DSH packaging deliberately afterward. Do not reuse or modify already
published assets. See [proposed bilingual release notes](hardening-release-notes.md).

Public tracked content was checked for personal checkout paths, private network
details, task transcripts and credentials. Reports contain reusable operations,
public source revisions and sanitized evidence, not private deployment state.
