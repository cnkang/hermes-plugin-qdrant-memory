# CI services

Candidate validation must name the plugin SHA and actual test execution. A green
optional Cloud workflow with missing credentials is SKIPPED, not Cloud validation.
The `qdrant-cloud` environment currently allows only `main`; keep this restriction.
Branch rejection is a release gate, not permission to weaken secret protection.
The compatibility matrix combines an immutable minimum reference with the moving
latest `main`; the weekly tracker adds the host sync-boundary checks.
Merged main `112e403c` passed CI, the platform matrix and executed authenticated
Cloud integration after the finalization PR #18 merged. The earlier `ff5712d`,
`f6a2e97` and `32bd4e7`
snapshots remain in the validation record.
See [validation evidence](validation.md) for exact-SHA run links and the
[acceptance report](releases/PRE_RELEASE_FINAL_REVIEW.md) for support limits.

Ruff lint/format, the Python 3.11 and 3.14 behavior-contract jobs, and the separate
Snyk dependency and code jobs run
independently in parallel. Both Python versions run files serially because remote
tests restart their disposable server. Python 3.14 collects coverage; SonarCloud
waits for the test matrix and consumes that artifact, then waits for its quality
gate. Snyk runs dependency and code scans and monitors main. The required test matrix
validates the support range endpoints: the minimum supported release v0.21.5
(v2026.9.24, `f97608f`) and the latest Hermes `main` (a moving ref; each run
records its checked-out SHA in the run summary). The latest-main lane
intentionally tracks the moving upstream ref inside the required matrix: an
upstream regression can fail the gate until it is fixed upstream, and a rerun of
the same commit can differ. A separately pinned PM prepares dependency
environments because the minimum release predates `pm.build_env`. Each lane runs
a digest-pinned Qdrant
v1.15.5 service and requires REST/gRPC integration, including restart persistence.
Ruff is pinned to 0.15.1 in CI and the local
[pre-commit setup](development.md); lint includes docstrings and import ordering.
Actions use immutable SHA pins. Authenticated scans run on same-repository PRs,
pushes to `main` and `codex/**`, and manual workflow dispatches. The credentialed
Cloud smoke lane is narrower: it runs only on `main` pushes (plus manual dispatch)
because branch pushes receive repository secrets, so `codex/**` branches are
excluded from it. Fork PRs run
tests without scanner credentials; their scanner jobs are skipped by conditions.
A missing required token fails a scanner job only when that job is scheduled.
The `Required security scan gate` runs with `always()` and fails for any failed,
cancelled or skipped prerequisite (lint, tests, SonarCloud and both Snyk jobs), including
safely skipped fork scans. Dependabot pull requests are the one deliberate
exception: they run lint, tests and the platform matrix, the credentialed
scanner jobs are skipped because Dependabot runs cannot read repository
secrets, and the gate accepts exactly those skips for `dependabot[bot]`; the
credentialed scans run on the resulting push to `main` after merge. Make this
aggregate gate a required branch check; a skipped scanner job alone is not proof
of a successful scan. The gate needs no secrets and executes no PR code.

## Dependency updates (Dependabot)

Dependabot opens grouped weekly version updates for the Python dependencies
and for the GitHub Actions pins (`.github/dependabot.yml`). Repository-level
security updates are enabled as well. Dependabot pull requests run the lint,
test and platform jobs; the credentialed scanner jobs are skipped for
`dependabot[bot]` and re-run on the push to `main` after merge.

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
checks out the current Hermes `main`, records the actual checked-out Hermes SHA and
plugin SHA in the run summary, then uses the host's canonical test runner for
directory discovery/CLI, setup, checkpoint/session lifecycle, shutdown, provider and
recovery contracts on Python 3.11 and 3.14. A separate direct pytest step checks the
host `MemoryManager.sync_all()` admission and shutdown boundary with
`HERMES_QDRANT_REQUIRE_HOST_SYNC=1`; the canonical runner clears unlisted environment
variables, so this required contract runs outside that runner. The dependency builder
remains pinned to the known PM commit while the tested host source follows `main`.
This workflow has read-only repository permissions, uses no credentials, and adds
the `MemoryManager.sync_all()` boundary checks on top of the required latest-main
lane. A green historical run applies only to the exact Hermes SHA printed in that
run summary.

## Platform compatibility

`.github/workflows/platform-compat.yml` runs the local behavior and host lifecycle
contracts on Ubuntu 24.04 LTS, macOS and Windows with Python 3.11 and 3.14. This matrix
checks platform-specific embedded Qdrant storage behavior alongside the main
behavior-contract jobs.

## Optional authenticated Cloud smoke

**Optional Qdrant Cloud smoke** runs on pushes to `main` only and supports
manual dispatch after the workflow reaches the default branch. The job uses the
`qdrant-cloud` GitHub Environment; configure that Environment to allow deployments
from `main` only. The job reads its credentials from that Environment, creates
only a disposable collection (isolated by a random UUID suffix), and never
passes or prints the values: it writes them to a private temporary file and
removes it at job completion.
Absent URL/key yields an explicit SKIPPED message. This manual lane is outside the
required scan gate; a skip does not establish Cloud compatibility. Its integration
test covers indexes, scope, mutation, migration and reconnection; deployment restart
remains a Server lane check because Cloud infrastructure is service-owned.

SonarCloud and Snyk use maintainer-managed repository secrets; contributors do
not need scanner credentials. Snyk runs dependency and code scans; high or
critical findings fail the job, and authentication errors also fail.
`requirements.txt` is the PM-exported runtime lock snapshot.

CodeRabbit reviews pull requests through its GitHub App, configured in
`.coderabbit.yaml`. Small repositories can require a manual review trigger, and
a skipped review check is not evidence of a completed code review.

Never paste secret values into issues, pull requests or logs. Missing tokens
produce explicit CI failures rather than a misleading green scan. Confirm actual
checks on a pull request before making them required branch protections.
