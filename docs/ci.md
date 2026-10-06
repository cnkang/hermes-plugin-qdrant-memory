# CI services

Ruff lint/format, the Python 3.11 and 3.14 behavior-contract jobs, and the separate
Snyk dependency and code jobs run
independently in parallel. Both Python versions run files serially because remote
tests restart their disposable server. Python 3.14 collects coverage; SonarCloud
waits for the test matrix and consumes that artifact, then waits for its quality
gate. Snyk runs dependency and code scans and monitors main. Tests use an
three immutable Hermes hosts: minimum v2026.9.24 (`f97608f`), pinned `4787e4d`,
and reviewed upstream main `3dadeb9`. A separately pinned PM prepares dependency
environments because the minimum release predates `pm.build_env`. Each lane runs
a digest-pinned Qdrant v1.15.5 service and requires REST/gRPC integration, including
restart persistence. Ruff is pinned to 0.15.1 in CI and the local
[pre-commit setup](development.md); lint includes docstrings and import ordering.
Actions use immutable SHA pins. Authenticated scans run on same-repository PRs,
pushes only to `main` and `codex/**`, and manual workflow dispatches. Fork PRs run
tests without scanner credentials; their scanner jobs are skipped by conditions.
A missing required token fails a scanner job only when that job is scheduled.
The `Required security scan gate` runs with `always()` and fails for any failed,
cancelled or skipped prerequisite (lint, tests, SonarCloud and both Snyk jobs), including
safely skipped fork scans. Make this
aggregate gate a required branch check; a skipped scanner job alone is not proof
of a successful scan. The gate needs no secrets and executes no PR code.

## Optional authenticated Cloud smoke

**Optional Qdrant Cloud smoke** runs on repository pushes to main/codex branches
and supports manual dispatch after the workflow reaches the default branch. Add
Actions secrets `QDRANT_CLOUD_URL` (HTTPS) and `QDRANT_CLOUD_API_KEY` (Database API
key with collection creation/deletion access). Optional
`QDRANT_CLOUD_COLLECTION_PREFIX` defaults to `hermes_smoke_`; a random UUID suffix
always isolates the test from existing collections. It creates and deletes only
that disposable collection. Credentials are written to a private temporary file,
never passed in process arguments or printed, and removed at job completion.
Absent URL/key yields an explicit SKIPPED message. This manual lane is outside the
required scan gate; a skip does not establish Cloud compatibility. Its integration
test covers indexes, scope, mutation, migration and reconnection; deployment restart
remains a Server lane check because Cloud infrastructure is service-owned.

SonarCloud project configuration:

- Organization key: `cnkang` (matching the account's existing SonarCloud setup).
- Project key: `cnkang_hermes-plugin-qdrant-memory`.
- Repository secret: `SONAR_TOKEN` with project analysis permission.
- Import the repository in SonarCloud and select CI analysis rather than duplicate
  automatic analysis. [Official GitHub Actions guide](https://docs.sonarsource.com/sonarqube-cloud/advanced-setup/ci-based-analysis/github-actions-for-sonarcloud).

Snyk needs repository secret `SNYK_TOKEN`, and the token's organization must have
Snyk Open Source and Snyk Code enabled. `requirements.txt` is the PM-exported
runtime lock snapshot. High/critical dependency or code findings fail the job;
authentication errors also fail. [Python CLI guide](https://docs.snyk.io/supported-languages/supported-languages-list/python/snyk-cli-for-python).

CodeRabbit runs through its GitHub App on pull requests. Grant the App access to
this repository in GitHub installation settings. `.coderabbit.yaml` enables
automatic reviews, including drafts, and review progress checks. It is not a
GitHub Actions executable and does not need a made-up CI token.
[Configuration reference](https://docs.coderabbit.ai/reference/configuration).

At onboarding, CodeRabbit reported that repositories with fewer than 10 stars
require a manual review trigger despite `auto_review.enabled`. A skipped review
check is not evidence of a completed code review.

Configure secrets through GitHub Settings → Secrets and variables → Actions or
`gh secret set`; never paste their values into issues, PRs or logs. Missing tokens
produce explicit CI failures rather than a misleading green scan. External App
authorization, project import and organization entitlements cannot be inferred
from committed config files. Confirm actual checks on the PR before making them
required branch protections.
