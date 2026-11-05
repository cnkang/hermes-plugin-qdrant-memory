# CI security audit

Audit date: 2026-11-05. Scope: every workflow, Dependabot configuration,
repository setting and third-party scanner that this repository uses.

The audit answers three questions for each control: which trust level may
trigger it, which credential it can reach, and what happens when it cannot run.
A control that cannot state all three is treated as a finding.

## Method

- Read every file under `.github/`, `scripts/` and the packaging configuration.
- Queried the GitHub API for branch protection, rulesets, environments,
  Actions permissions, secret names (never values), fork approval policy,
  artifact retention and the last 30 days of workflow runs.
- Ran actionlint 1.7.12, zizmor 1.32.0 and gitleaks 8.30.1 locally against the
  same tree that CI uses.
- Replayed the required gate against simulated job results, including failure,
  cancellation, skip and missing-job payloads.
- Checked the published free-tier terms for every third-party service. The
  citations and the resulting policy are in
  [CI free-tier policy](ci-free-tier-policy.md).

## Workflow inventory

| Workflow | Trigger | Credential | Trust level | Gate |
| --- | --- | --- | --- | --- |
| `ci.yml` | push `main`/`codex/**`, PR, dispatch | `SONAR_TOKEN`, `SNYK_TOKEN` in named jobs only | Tier 1 plus Tier 2 | `Required security scan gate` |
| `codeql.yml` | push `main`, PR, weekly, dispatch | `security-events: write` | Tier 1 | Independent; required by the release gate |
| `platform-compat.yml` | push `main`/`codex/**`, PR, dispatch | none | Tier 1 | Job is a required check candidate |
| `standalone.yml` | push `main`/`codex/**`, PR, dispatch | none | Tier 1 | Job is a required check candidate |
| `packaging.yml` | push `main`/`codex/**`, PR | none | Tier 1 | Independent |
| `shared-security.yml` | push `main`/`codex/**`, PR, dispatch | none | Tier 1 | Required by the release gate |
| `local-embedding.yml` | push `main`/`codex/**`, PR, dispatch | none | Tier 2 (CPU model download) | Independent |
| `pi-quality-driver.yml` | push `main` on selected paths | none | Tier 2 | Independent |
| `publish.yml` | tag `v*`, dispatch | OIDC `id-token`, `attestations` | Tier 4 | `check_release_gate.py` |
| `cloud-smoke.yml` | push `main`, dispatch | `qdrant-cloud` environment | Tier 3 | Manual |
| `soak.yml` | dispatch, weekly | `soak-local`, `soak-cloud` | Tier 3 | Internal admission gates |
| `codex-plugin-cloud.yml` | dispatch | `soak-cloud` | Tier 3 | Manual |
| `owner-model-quality.yml` | `workflow_call` | `model-quality`/`model-smoke` | Tier 3 | Internal |
| `owner-model-smoke.yml` | selected paths, dispatch | none | Tier 3 | Independent |
| `upstream-compat.yml` | weekly, dispatch | none | Tier 3 | Independent |
| `scheduled-security.yml` | weekly, main dispatch | `SNYK_TOKEN` | Tier 3 | Independent |
| `scorecard.yml` | push `main`, weekly, dispatch | OIDC, read-only token | Tier 3 | Never gates |
| `retry-infrastructure.yml` | `workflow_run` | `actions: write` | Trusted controller | Never gates |
| `ci.yml` secret scan / workflow security / dependency review | any push or PR | none | Tier 0 | Required |

Tier definitions match the hardening plan: Tier 0/1 is untrusted-input safe and
credential free, Tier 2 is trusted-tree integration, Tier 3 is scheduled or
manually dispatched expensive work, Tier 4 is release.

## Trust boundary analysis

**Fork pull requests.** They receive no repository secret, a read-only
`GITHUB_TOKEN` and no OIDC identity. The jobs that need a secret are skipped by
an `if` that compares `github.event.pull_request.head.repo.full_name` with the
repository. No workflow uses `pull_request_target`, so no fork workflow runs in
the base repository context.

**Same-repository pull requests.** A branch author with write access can edit a
workflow that holds a scanner token. Write access is the GitHub trust boundary,
so this is accepted. Least-privilege tokens and the protected environments bound
the exposure.

**`workflow_run`.** `retry-infrastructure.yml` is the only consumer. It rejects
events whose `head_repository` is not this repository, never checks out pull
request content, downloads its controller from `github.workflow_sha` (the
trusted default-branch copy), and only calls `rerun-failed-jobs`. It cannot
execute contributor code.

**Artifacts.** Coverage artifacts cross a job boundary inside one run. They are
produced by credential-free jobs and consumed by SonarCloud, which treats them
as XML. No privileged job consumes an artifact produced by an untrusted job, and
no `workflow_run` consumes artifacts.

**Caches.** No workflow uses `actions/cache`, and the release workflow reads no
cache. `setup-node` caching is not enabled anywhere in this repository.

**OIDC.** Only `publish.yml` and `scorecard.yml` request `id-token: write`, both
behind a tag or a trusted branch, and both with a protected environment or a
non-gating purpose.

## Findings

| ID | Severity | Finding | Status |
| --- | --- | --- | --- |
| F-01 | High | `scan-gate` required SonarCloud success for fork pull requests, but SonarCloud skips every fork. Every external pull request failed the required check. | Fixed. `scripts/ci_gate.py` evaluates `toJSON(needs)` and allows a skip only for a named credential-gated job in the one context that forbids the credential. |
| F-02 | High | zizmor `bot-conditions`: the Dependabot skip used `github.actor`, which the last actor can change. | Fixed. The condition now reads `github.event.pull_request.user.login`. |
| F-03 | Medium | Snyk Code ran on every same-repository pull request and every `codex/**` push, in addition to every `main` push. That is a metered service called several times per commit. | Fixed. Both Snyk jobs run only from `main`; `scheduled-security.yml` adds a weekly scan. |
| F-04 | Medium | No pull request compared dependency changes, and no workflow linted or audited the workflow files themselves. | Fixed. `Dependency review`, `Workflow security analysis` (actionlint plus zizmor) and `Secret scan` (gitleaks) are required jobs. |
| F-05 | Medium | The release gate accepted a tag whose CodeQL run failed. | Fixed. `CodeQL` is a required workflow in `scripts/check_release_gate.py`. |
| F-06 | Medium | Distributions had no provenance statement outside PyPI Trusted Publishing. | Fixed. `publish.yml` attests `dist/*` with `actions/attest-build-provenance`. |
| F-07 | Low | Five `run:` blocks interpolated step outputs directly, and the retry controller path could in principle contain shell metacharacters. | Fixed. All five now pass through `env`. |
| F-08 | Low | Dependabot adopted new releases immediately. | Fixed. Seven-day cooldown on both ecosystems. |
| F-09 | Medium | Fork pull request workflows ran automatically for returning contributors (`first_time_contributors`). | Fixed. The repository now approves every external contributor's workflow run before it starts (`all_external_contributors`). |
| F-10 | Accepted | `main` requires zero approving reviews. A solo maintainer cannot approve their own pull request. | Accepted with CODEOWNERS as the review prompt. Revisit when a second maintainer joins. |
| F-11 | Accepted | `actions/permissions` allows all actions and does not require SHA pinning at the organization level. | Accepted: every `uses:` is already pinned to a full commit SHA, and zizmor enforces it on every run. |
| F-12 | Accepted | `scan-gate` executes `scripts/ci_gate.py` from the pull request tree. | The gate job holds no secret and a read-only token, and the gate treats a missing job as failure, so a removed or disabled test job cannot pass. Workflow changes still need review. |

## Repository settings

Verified through the API on 2026-11-05:

| Setting | Runtime |
| --- | --- |
| Default workflow permissions | read |
| Workflow can approve pull requests | no |
| Secret scanning | enabled |
| Push protection | enabled |
| Dependabot security updates | enabled |
| Artifact and log retention | 90 days |
| Fork workflow approval | `all_external_contributors` |
| Required check on `main` | `Required security scan gate`, strict |
| Force push / deletion | blocked |
| Release tag ruleset | immutable `v*` tags, active |

## Run baseline (30 days)

Counts come from the Actions API on 2026-11-05. Durations are wall-clock median
and p95 of completed runs.

| Workflow | Runs | Success | Cancelled | Failure | Median | p95 |
| --- | --- | --- | --- | --- | --- | --- |
| CI | 100+ | 50% | 50% | 0% | 16 min | 26 min |
| Platform compatibility | 100+ | 52% | 47% | 1% | 12 min | 22 min |
| Standalone compatibility | 100+ | 49% | 51% | 0% | 12 min | 21 min |
| CodeQL | 100+ | 91% | 9% | 0% | 3 min | 12 min |
| Shared destination security | 100+ | 81% | 19% | 0% | 4 min | 14 min |
| Runtime packaging | 100+ | 93% | 7% | 0% | 8 min | 14 min |
| Optional local embedding | 100+ | 74% | 26% | 0% | 5 min | 17 min |
| Optional Qdrant Cloud smoke | 100+ | 97% | 2% | 1% | 3 min | 10 min |
| Retry controller | 100+ | 9% | 0% | 0% | 1 s | 13 s |
| Publish Runtime | 12 | 100% | 0% | 0% | 11 min | 14 min |
| Latest Hermes compatibility | 12 | 92% | 8% | 0% | 1 min | 3 min |

Cancellations are dominated by `concurrency: cancel-in-progress` on the
`codex/**` dual lane, where a push and its pull request run the same matrix.
Retaining both lanes is deliberate: it produces push-side and pull-request-side
evidence for the same commit. The cost is a duplicate matrix, not a duplicate
paid-service call, because the credentialed jobs are now `main`-only.

The retry controller creates about 90 skipped runs per 30 days, one per
completed run of the four watched workflows. A skipped job does not allocate a
runner, so the cost is run-list noise, not minutes.

## Verification

- `pytest tests/test_ci_gate.py`: 60 passed. It executes the gate script and
  covers success, failure, skipped, cancelled, missing-job, fork, Dependabot,
  main-push and dependency-review cases.
- actionlint 1.7.12 on the whole tree: clean.
- zizmor 1.32.0 offline, regular persona: no findings, with three reviewed
  file-scoped exceptions in `.github/zizmor.yml`.
- gitleaks 8.30.1 over the full history identified two synthetic test fixtures
  in `tests/` and no production finding. The workflow ignores only their exact
  historical fingerprints in `.gitleaksignore`; a changed commit, path, rule or
  line is scanned normally. The scan report is not committed. GitHub secret
  scanning and push protection keep covering the full history without a
  workflow artifact.

## Manual actions

1. Snyk dashboard: confirm the three projects are attached to a public
   repository and to the expected organization, confirm the Git remote URL, and
   disable automatic pull request tests for this repository. Details are in
   [CI free-tier policy](ci-free-tier-policy.md).
2. Optional: require a code-owner review once a second maintainer exists.
   Fork workflow approval is already `all_external_contributors`; rollback is
   `-f approval_policy=first_time_contributors`.

## Residual risks

- A tag can still be created on a commit whose required workflows passed and
  whose code has since been superseded. The gate verifies the tagged commit, not
  the branch tip; that is the intended semantic.
- SonarCloud and Snyk availability remain external dependencies of `main`
  pushes. An outage fails the run rather than passing silently, which is the
  intended fail-closed behavior.
- A secret that entered history before push protection was enabled is visible
  to GitHub secret scanning but not to the per-event gitleaks job, which scans
  only the commits an event introduces. The one-time history audit found two
  synthetic fixtures and no production credential.
