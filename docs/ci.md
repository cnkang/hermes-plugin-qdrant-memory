# CI services

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
gate. Snyk runs dependency and code scans and monitors main. The required test matrix
validates the support range endpoints. It covers the minimum supported release
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
Actions use immutable SHA pins. Authenticated scans run on same-repository PRs,
pushes to `main` and repository development branches, and manual workflow dispatches.
The credentialed Cloud smoke lane is narrower: it runs only on `main` pushes
(plus manual dispatch). Branch pushes receive repository secrets, so the lane
excludes them.
Fork PRs run
tests without scanner credentials. Their scanner jobs are skipped by conditions.
A missing required token fails a scanner job only when that job is scheduled.
The `Required security scan gate` runs with `always()` and fails for any failed,
cancelled or skipped prerequisite (lint, tests, MCP protocol coverage, host pin compatibility, SonarCloud and both Snyk jobs), including
safely skipped fork scans. Dependabot pull requests are the one deliberate
exception. They run lint, tests and the platform matrix. Their credentialed
scanner jobs are skipped because Dependabot runs cannot read repository
secrets, and the gate accepts exactly those skips for `dependabot[bot]`. The
credentialed scans run on the resulting push to `main` after merge. Make this
aggregate gate a required branch check. A skipped scanner job alone is not proof
of a successful scan. The gate needs no secrets and executes no PR code.

## Dependency updates (Dependabot)

Dependabot opens grouped weekly version updates for the Python dependencies
and for the GitHub Actions pins (`.github/dependabot.yml`). Repository-level
security updates are enabled as well. Dependabot pull requests run the lint,
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
directory discovery/CLI, setup, checkpoint/session lifecycle, shutdown, provider and
recovery contracts on Python 3.11 and 3.14. A separate direct pytest step checks the
host `MemoryManager.sync_all()` admission and shutdown boundary with
`HERMES_QDRANT_REQUIRE_HOST_SYNC=1`. The canonical runner clears unlisted environment
variables, so this required contract runs outside that runner. The dependency builder
remains pinned to the known PM commit while the tested host source follows `main`.
This workflow has read-only repository permissions, uses no credentials, and adds
the `MemoryManager.sync_all()` boundary checks on top of the required latest-main
lane. A green historical run applies only to the exact Hermes SHA printed in that
run summary.

## Host pin compatibility

The **Host pin compatibility** job reconciles every host pin for a package
this plugin also requires, including pins in optional extras and dependency
groups, with the plugin's dependency specifiers on the minimum and latest host
checkouts. It runs under Python 3.11 and 3.14 and records each resolved host
revision in the run summary. It then co-resolves the host pins for shared
packages together with the plugin's optional extras through the pinned
dependency builder. A pin outside the plugin's range, or a resolution that
cannot satisfy both sides, fails the job. This mirrors the dependency manager's
install-time co-resolution of the plugin inside the host workspace. The
required security scan gate includes this job.

## Platform compatibility

`.github/workflows/platform-compat.yml` runs the local behavior and host lifecycle
contracts on Ubuntu 24.04 LTS, macOS and Windows with Python 3.11 and 3.14. This matrix
checks platform-specific embedded Qdrant storage behavior alongside the main
behavior-contract jobs.

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
not need scanner credentials. Snyk runs dependency and code scans. High or
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
