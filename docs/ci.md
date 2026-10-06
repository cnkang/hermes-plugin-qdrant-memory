# CI services

The CI workflow runs behavior contracts against an immutable Hermes host commit
on Python 3.11 and 3.14, then passes coverage to SonarCloud. Sonar analysis waits
for its quality gate. Snyk runs dependency and code scans and monitors main.
Actions use immutable SHA pins. Authenticated scans run on same-repository PRs,
pushes only to `main` and `codex/**`, and manual workflow dispatches. Fork PRs run
tests without scanner credentials; their scanner jobs are skipped by conditions.
A missing required token fails a scanner job only when that job is scheduled.
The `Required security scan gate` runs with `always()` and fails for any failed,
cancelled or skipped prerequisite, including safely skipped fork scans. Make this
aggregate gate a required branch check; a skipped scanner job alone is not proof
of a successful scan. The gate needs no secrets and executes no PR code.

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
