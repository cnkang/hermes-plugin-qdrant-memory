# CI services

## Optional CPU dependency snapshot

The Linux Python 3.11 CPU lane installs `requirements-local.txt` with hashes
before the optional provider tests, without requiring Snyk credentials. This
checks the same frozen snapshot used by the security job, including Dependabot
PRs. Resolving `.[local]` alone does not validate that snapshot. All CPU lanes
also run `pip check` after installing their test dependencies.

FastEmbed 0.9.1 includes upstream support for Pillow 13, which contains fixes for
advisories in Pillow 12.3.0. The optional CPU snapshot pins the official 0.9.1
wheel hashes. CI installs that package directly, requires Pillow 13.0.0, and
runs `pip check`. No local metadata patch or compatibility wheel is needed.
The provider accepts exactly FastEmbed 0.9.1, and its pipeline identity includes
that pinned backend version.

All three CPU CI lanes run the provider contracts and real offline CPU model
acceptance, including CRUD, restart, retrieval and MCP. The Linux Python 3.11
security job installs and scans the same hash-checked snapshot on `main`. Hub
stays at 1.33.0: upgrading it to 2 is a separate compatibility change.

For an equivalent disposable local check:

```sh
python -m pip install --require-hashes --only-binary=:all: -r requirements-local.txt
python -m pip install --only-binary=:all: '.[mcp,local]' 'Pillow==13.0.0' 'pytest>=8,<10'
python -m pip check
```

Security scans remain authoritative for their configured gate. Findings are not
automatically ignored. The offline model acceptance continues to verify the
prepared artifacts and real inference path.

Dependency PR #111 advances the PR #72 pins to Pydantic 2.14.2 and
pydantic-core 2.50.2 in the main and quality snapshots.
Hash enforcement and dependency security scans are not bypassed.

## Host dependency overrides

The host compatibility jobs use a pinned PM with lock override support. They
pass the tested host's `uv.lock` to each environment that installs exported host
requirements. This preserves the host's security overrides, including
`Pillow==13.0.0`. An export alone does not carry these overrides. PM checks
installed dependencies against that lock and still rejects unrelated conflicts.

## Transient infrastructure retries

The validation workflows have a separate, bounded retry controller. It permits
at most two additional run attempts (three attempts total). It classifies every
failed job before it requests a rerun. It requests `rerun-failed-jobs`, so a
successful matrix job never runs again.

A failed job qualifies only under one of these evidence classes:

- `TRANSIENT_BOOTSTRAP`: the job stopped in `Set up job`, and its log shows a
  failed Action archive download plus a strict DNS, timeout or service error.
- `TRANSIENT_NETWORK`: an allowlisted GitHub action step
  (`checkout`, `setup-python`, `upload-artifact` or `download-artifact`) failed,
  every earlier step succeeded, and the log shows a strict transient network or
  service error.
- `DEPENDENT_GATE`: the aggregate gate failed only to restate a prerequisite.
  It is rerun only with at least one eligible root-cause job.

The classifier fails closed. A test failure, a dependency conflict, a `404`, a
missing credential, a permission error and an unreadable log never qualify.
A `TimeoutError` from Python test code is not network evidence. Quality, lint,
dependency and security jobs are never retried, even when their logs contain a
timeout. A run with mixed retryable and non-retryable failures is not rerun.
Fork pull requests, foreign heads, stale events and exhausted budgets are
rejected. A failed controller does not rerun anything and cannot trigger
itself.

The controller reads fresh run and attempt metadata before the request and
again after the classification. It compares the event's attempt and commit SHA
with the live run. Per-attempt concurrency serializes duplicate events. A
successful rerun is recorded separately, so the failed attempt keeps its
original logs and check result.

### Writer timeout diagnostics

`QdrantMemoryProvider.timeout_report()` prints one bounded
`QDANT_WRITER_TIMEOUT` JSON line and returns it. Use it to diagnose a slow or
stalled writer drain. The report holds provider state, worker liveness and
busy state. It also holds queue depth, ledger
status counts, last progress age, recent job durations and a file and function
stack snapshot. It never contains memories, prompts, payloads or credentials. It
takes two samples two seconds apart, so a slow writer is distinguishable from a
stalled one. Counts come from a separate read-only SQLite connection, so the
diagnostics do not compete for the writer lock.

`tests/test_provider.py::test_100_fast_turns_no_loss_and_replay` uses this report
on its 180-second timeout. The specific Windows durability timeout remains
non-retryable. The controller records `TEST_PERFORMANCE_TIMEOUT` and preserves
the evidence. Automatic retry for this test stays deferred until a real run
proves durable progress, no terminal failure and a consistent ledger.

The controller writes a Markdown summary and a JSON artifact for every failure
and every retried run. The record carries the run and commit identity. It lists
each failed job, its category and basis, and each attempt. It records the final
status and any automatic retry. Final statuses are `PASS_FIRST_ATTEMPT`,
`PASS_AFTER_INFRA_RETRY`, `PASS_AFTER_PERFORMANCE_RETRY`,
`PASS_AFTER_UNVERIFIED_RETRY`, `FAIL_NON_RETRYABLE`, `FAIL_RETRY_EXHAUSTED`
and `INCONCLUSIVE`. The audit emits `PASS_FIRST_ATTEMPT` only when it is run
directly for a first-attempt success. The scheduled controller reports failures
and retried runs, so normal first-attempt successes have no audit record.

The controller reads only run metadata and job logs. It never checks out or
executes the triggering head, and it never consumes its artifacts. It reads log
text only to classify a failure.
It fetches its own script through the contents API from the retry workflow's
trusted commit. The fetch permits three download attempts at most. Release and
publish workflows are excluded.

GitHub activates `workflow_run` workflows only after their file exists on the
default branch. Adding this controller in a PR does not activate it before merge.
See [workflow event behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
and [failed-job rerun API](https://docs.github.com/en/rest/actions/workflow-runs#re-run-failed-jobs-from-a-workflow-run).

## Release gate

`publish.yml` requires successful runs of `CI`, `CodeQL`, `Platform compatibility`,
`Standalone compatibility` and `Shared destination security` for the exact
tagged commit on `main`. A missing, failed or still-running workflow blocks the
build before it creates any distribution. A tag can no longer publish a commit
while the Windows matrix is red. The check reads workflow metadata with
`actions: read` and then stops. It does not change Trusted Publishing, OIDC or
environment approvals. A release from an older commit needs green required
workflows for that commit, or a newer commit. The workflow file is read from the
tagged commit. Tag a commit that contains this gate. A tag on an older commit
uses that commit's workflow file and cannot receive this check.

See [standalone CLI and MCP](standalone.md) for the additive host-independent
interface, shared durable core, fixed operator scope and its validation boundary.

Candidate validation must name the plugin SHA and actual test execution. A green
optional Cloud workflow with missing credentials is SKIPPED, not Cloud validation.
Configure the `qdrant-cloud` environment with approved-ref restrictions and
appropriate approval requirements. Manual dispatch can select another ref.
Workflow triggers alone do not protect credentials. Keep credential access
restricted when testing a release.
The compatibility matrix combines an immutable minimum reference with the moving
latest `main`. The weekly tracker adds the host sync-boundary checks.
Merged main `112e403c` passed CI, the platform matrix and executed authenticated
Cloud integration after the finalization PR #18 merged. The earlier `ff5712d`,
`f6a2e97` and `32bd4e7`
snapshots remain in the validation record.
See [validation evidence](validation.md) for exact-SHA run links and the
[release notes](releases/v0.1.0.md) for the published release's support limits.

Ruff lint/format, the Python 3.11 and 3.14 behavior-contract jobs, and the separate
Snyk dependency and code jobs run
independently in parallel. Both Python versions run files serially because remote
tests restart their disposable server. Python 3.14 collects coverage. SonarCloud
waits for the test matrix and consumes that artifact, then waits for its quality
gate. The Snyk jobs in `ci.yml` run on pushes to `main` and manual dispatches
targeting `main`; their `monitor` steps run only on pushes. The required test
matrix validates the support range endpoints. It covers the minimum supported release
v0.21.5 (v2026.9.24, `f97608f`) and the latest Hermes `main`. The `main` ref
moves, and each run records its checked-out SHA in the run summary. The latest-main lane
intentionally tracks the moving upstream ref inside the required matrix. An
upstream regression can fail the gate until it is fixed upstream, and a rerun of
the same commit can differ. A separately pinned PM prepares dependency
environments because the minimum release predates `pm.build_env`. Each lane runs
a digest-pinned Qdrant
v1.15.5 service and requires REST/gRPC integration, including restart persistence.
Ruff is pinned to 0.15.1 in CI and the local
[pre-commit setup](development.md). Lint includes docstrings and import ordering.
Actions use immutable SHA pins. Both Snyk jobs hold the organization token and
run only on `main` pushes or manual dispatches targeting `main`. Pull requests
and branch pushes verify the dependency inputs with the credential-free
`dependency-lock` job and `Dependency review`
instead. The lock job rejects ranges, remote or VCS sources, option lines,
unpinned entries, unhashed snapshot entries and snapshot drift. Dependency review
fails a pull request that adds a high or critical advisory, or a dependency under
a denied strong-copyleft license. `scheduled-security.yml` repeats the Snyk Code
scan weekly and on maintainer dispatch, so a scan can exist without a push.
The dependency scan is therefore a post-merge gate: a failure on `main` blocks
later work and needs a fix-forward, because the merge already happened. A
repository writer can still edit a workflow that holds a scanner token, because
write access is the GitHub trust boundary. Least-privilege scanner tokens and
protected environments bound that exposure, and the dependency scan no longer
adds a manifest-only path to it.
The credentialed Cloud smoke lane is narrower: it runs only on `main` pushes
(plus manual dispatch). Branch pushes receive repository secrets, so the lane
excludes them.
Fork PRs run
tests without scanner credentials. Their scanner jobs are skipped by conditions.
A missing required token fails a scanner job only when that job is scheduled.
The `Required security scan gate` runs even after workflow cancellation.
It rejects failed, skipped, or cancelled prerequisites.
It requires successful lint, tests, MCP protocol coverage, Codex Windows security,
host pin compatibility, dependency lock, workflow security, secret scanning and,
on pull requests, dependency review. It requires SonarCloud on trusted trees and
requires the Snyk jobs on `main`.
[GitHub permits a merge when a required job skips execution](https://docs.github.com/en/pull-requests/reference/status-checks).
The gate uses `always()` and reads `toJSON(needs)` through `scripts/ci_gate.py`.
It accepts a skip only for a named credential-gated job in the one context that
forbids the credential, and it rejects a successful credentialed scan in an
untrusted context: that would mean the token reached contributor code. A fork
pull request therefore passes with the token-free jobs green and fails on any
skipped test job.

Dependabot pull requests are the one deliberate exception. They run
lint, tests and the platform matrix. Their credentialed
scanner jobs are skipped because Dependabot runs cannot read repository
secrets, and the gate accepts exactly those skips for `dependabot[bot]`. The
credentialed scans run on the resulting push to `main` after merge. Make this
aggregate gate a required branch check. A skipped scanner job alone is not proof
of a successful scan. The gate needs no secrets and executes no PR code.

## Dependency updates (Dependabot)

Dependabot opens grouped weekly version updates for the Python dependencies
and for the GitHub Actions pins (`.github/dependabot.yml`). Every ecosystem uses
a seven-day cooldown, so a freshly published release is not adopted on the day
it appears. Repository-level security updates are enabled as well. Dependabot pull requests run the lint,
test and platform jobs. The credentialed scanner jobs are skipped for
`dependabot[bot]` and re-run on the push to `main` after merge. A dependency
range change also requires the plugin manifest's `python_dependencies` to be
updated in the same pull request. The manifest contract test enforces the
match.

## Code scanning (CodeQL)

A separate CodeQL workflow analyzes the plugin's Python sources and its
GitHub Actions workflows with the `security-extended` query suite. It runs on
pushes to `main`, on pull requests and on a weekly schedule. Results appear
under the repository's Security tab as code-scanning alerts. The workflow
uses immutable action pins and read-only repository permissions, plus
`security-events: write` for the upload. It is independent of the required
security scan gate, runs on its own workflow and consumes no repository
secrets.

## Latest Hermes tracking

`.github/workflows/upstream-compat.yml` runs weekly and through manual dispatch. It
checks out the current Hermes `main` and records the checked-out Hermes SHA and
plugin SHA in the run summary. It then uses the host's canonical test runner for
directory discovery and CLI, setup, checkpoint and session lifecycle, shutdown,
provider and recovery contracts. These tests run on Python 3.11 and 3.14. A
separate direct pytest step checks the host `MemoryManager.sync_all()` admission
and shutdown boundary with `HERMES_QDRANT_REQUIRE_HOST_SYNC=1`. The canonical
runner clears unlisted environment variables, so this required contract runs
outside that runner. The dependency builder remains pinned to the known PM commit
while the tested host source follows `main`. This workflow has read-only repository
permissions, uses no credentials, and adds the `MemoryManager.sync_all()` boundary
checks on top of the required latest-main lane. A green historical run applies only
to the exact Hermes SHA printed in that run summary.

## Host pin compatibility

The **Host pin compatibility** job reconciles every host pin with the plugin's
dependency specifiers. It covers each package that the plugin also requires,
including pins in optional extras and dependency groups. It checks the minimum and
the latest host checkouts. It runs under Python 3.11 and 3.14 and records each
resolved host revision in the run summary. It then co-resolves the host pins for
shared packages together with the plugin's optional extras through the pinned
dependency builder. A pin outside the plugin's range, or a resolution that
cannot satisfy both sides, fails the job. This mirrors the dependency manager's
install-time co-resolution of the plugin inside the host workspace. The
required security scan gate includes this job.

## Platform compatibility

`.github/workflows/platform-compat.yml` runs the local behavior and host lifecycle
contracts on Ubuntu 24.04 LTS, macOS and Windows with Python 3.11 and 3.14. This matrix
checks platform-specific embedded Qdrant storage behavior alongside the main
behavior-contract jobs.

## Staged soak workflow

**Soak** (`.github/workflows/soak.yml`) runs from manual dispatch and a weekly
schedule only. It is not a pull request lane, so no contributor change can reach
a cloud credential. The workflow uses two environments: `soak-local` for the
local Qdrant matrix and `soak-cloud` for the optional cloud window. Both allow
`main` only. The local matrix runs six hosted legs (x64 and ARM64, three
scenarios) for eight minutes each. An independent gate must pass before a
five-hour matrix may start. The cloud window keeps the required reviewer, so a
human approves it before it runs. See [soak testing](testing/soak.md) for the
topology, the invariants, the credential contract and the exact dispatch
commands.

## Optional authenticated Cloud smoke

**Optional Qdrant Cloud smoke** runs on pushes to `main` only and supports
manual dispatch after the workflow reaches the default branch. The job uses the
`qdrant-cloud` GitHub Environment. Configure that Environment to allow deployments
from `main` only. The job reads its credentials from that Environment. It creates
only a disposable collection (isolated by a random UUID suffix) and never
passes or prints the values. It writes them to a private temporary file and
removes it at job completion.
Absent URL/key yields an explicit SKIPPED message. This manual lane is outside the
required scan gate. A skip does not establish Cloud compatibility. Its integration
test covers indexes, scope, mutation, migration and reconnection. Deployment restart
remains a Server lane check because Cloud infrastructure is service-owned.

SonarCloud and Snyk use maintainer-managed repository secrets. Contributors do
not need scanner credentials. The `ci.yml` Snyk jobs run only on `main` pushes or
manual dispatches targeting `main`; `scheduled-security.yml` adds a weekly scan
and a manual scan restricted to `main`. High or
critical findings fail the job, and authentication errors also fail.
`requirements.txt` is the PM-exported runtime lock snapshot.

Automated review can require a manual trigger in small repositories. A skipped
review check is not evidence of a completed code review.

Never paste secret values into issues, pull requests or logs. Missing tokens
produce explicit CI failures rather than a misleading green scan. Confirm actual
checks on a pull request before making them required branch protections.


### Shared loopback HTTP review

The opt-in local service has a documented, rule/resource-scoped `python:S5332`
exception in `sonar-project.properties`. Review the [transport decision and its
expiry conditions](shared-memory/architecture.md#reviewed-local-http-transport-decision-sonar-s5332)
with the shared-service PR. This does not change the quality thresholds or exempt
other security analysis. Remove the exception before introducing a remote listener and complete a new
transport and authorization review.

## Workflow security and secret scanning

`Workflow security analysis` and `Secret scan` run on every push and pull
request. `Dependency review` runs on pull requests, including forks. All three
are credential-free, so these checks can safely process untrusted pull requests.

- `Workflow security analysis` runs actionlint 1.7.12 and zizmor 1.32.0 over
  `.github/`. zizmor fails on any finding. `.github/zizmor.yml` holds three
  reviewed, file-scoped exceptions: the `workflow_run` retry controller, the
  pinned global CLI in the Codex cloud probe, and the `./` reusable-workflow
  form. Rules are never disabled globally.
- `Secret scan` runs gitleaks 8.30.1 over the commits that the event
  introduced. It prints findings with `--redact`, so a value never reaches a
  log, and it writes no artifact. The root `.gitleaksignore` contains exact
  fingerprints for the two audited synthetic test fixtures; other findings
  remain blocking. A synthetic fixture that trips a new rule gets an
  inline `gitleaks:allow` comment with a reason; a scan report is never
  committed, because it carries commit metadata. Full-history detection stays
  with GitHub secret scanning and push protection, which need no workflow and
  are enabled on this repository.
- `Dependency review` runs on pull requests, fails on high or critical
  advisories, and denies AGPL-3.0, GPL-2.0, GPL-3.0 and SSPL-1.0 licenses. It
  reads the dependency graph that GitHub maintains for the repository.

Both binaries are downloaded from a GitHub release over HTTPS and verified
against a committed SHA-256 before use. The zizmor action is pinned to a full
commit SHA and to an exact zizmor version.

`scheduled-security.yml` repeats the Snyk Code scan weekly and on maintainer
dispatch. That is the only credentialed scan outside a `main` push, and no pull
request can trigger it.

`scorecard.yml` publishes an OpenSSF Scorecard result weekly, on `main` pushes
and on dispatch. It uses the workflow's own OIDC identity and a read-only
repository token. It never gates a pull request, and no long-lived token is
created to raise a score.

Free-tier limits, trigger counts and the settings that only the maintainer can
change are recorded in [CI free-tier policy](ci-free-tier-policy.md). The
current risk register and trust boundaries are in
[CI security audit](ci-security-audit.md).
