# CI free-tier policy

Every service used by this repository must have a written free-use basis, a
known trigger, and a defined failure mode. A service without all three is not a
merge gate.

Policy checked on 2026-11-05. Re-check the linked pages before adding a
service or changing a trigger.

## Services and free-use basis

| Service | Free basis | Metered? | Can a fork PR trigger it? |
| --- | --- | --- | --- |
| GitHub Actions (standard runners) | Free for public repositories | No billing for standard runners | Yes, but only credential-free jobs |
| GitHub CodeQL | Code scanning is free for public repositories | No | Yes |
| GitHub Dependency Review | Free for public repositories; needs the dependency graph | No | Yes |
| GitHub secret scanning and push protection | Free for public repositories | No | Push protection applies to every push |
| GitHub artifact attestations | Free for public repositories | No | No (tag or trusted branch only) |
| zizmor | Open source, runs offline, no account | No | Yes |
| actionlint | Open source, no account | No | Yes |
| gitleaks (CLI) | Open source, MIT, no account | No | Yes |
| OpenSSF Scorecard | Open source action; publishing uses the workflow's OIDC identity | No | No (push/schedule on main; manual dispatch is main-only) |
| SonarCloud | Free for public repositories | Analysis is unmetered for public projects; no card required | No (token held by the maintainer) |
| Snyk | Free plan; public repositories are exempt from test limits | Partly: see below | No |
| CodeRabbit | Open-source plan for public repositories | Partly: see below | It may comment, but it is not a gate |
| Sourcery | Free forever for public repositories | No for public repos | It may review, but it is not a gate |
| Socket | Free for open source | No for public repos | It may comment, but it is not a gate |

## Snyk

Official basis:

- <https://snyk.io/plans/> lists the Free plan at $0 with 5 projects, Snyk Code
  100 tests per month and Snyk Open Source 200 tests per month.
- <https://docs.snyk.io/snyk-data-and-governance/what-counts-as-a-test> states
  that the applicable test limits apply to **private repositories only**.
- <https://snyk.io/open-source/> describes a separate Secure Developer Program.
  It requires an application, approval, a README link back to Snyk and a logo
  licence. **This repository does not assume that entitlement.**

Consequences that this policy relies on:

1. Test limits do not apply to a public repository. That is the documented rule,
   not an observed account state.
2. The Free plan still limits the number of **projects**. `snyk monitor` creates
   or updates a project; `snyk test` does not. This repository runs `monitor`
   for `requirements.txt` and `requirements-local.txt` only, and never changes
   `--project-name` or `--target-reference` without checking the project count
   in the dashboard first. Changing either flag creates a new project.
3. Two test counts exist and are counted separately: Snyk Code and Snyk Open
   Source. A single job that scans both would consume both.
4. `--remote-repo-url` is documented for `snyk test`. It attributes the result
   to the public repository URL. It is **not** a guarantee of unmetered use, and
   it is not accepted by `monitor` in the documented option list, so `monitor`
   keeps its default target.

Trigger policy:

| Job | Trigger | Calls per trigger |
| --- | --- | --- |
| `snyk` (Open Source) | push to `main`; manual dispatch on `main` | two `test` calls; two `monitor` calls on push only |
| `snyk-code` | push to `main`; manual dispatch on `main` | one `snyk code test`, at most three attempts |
| `Snyk Code weekly scan` | weekly schedule; manual dispatch on `main` | one `snyk code test`, at most two attempts |

No pull request, non-main branch push or matrix leg calls Snyk. Both workflows
skip their credentialed scans when manually dispatched against a non-main ref.
The two `monitor` calls in `ci.yml` run only on a push to `main`. The token is a
repository secret that GitHub does not expose to fork pull requests.

Failure classification:

| Result | Meaning | Behaviour |
| --- | --- | --- |
| exit 0 | PASS | job succeeds |
| exit 1 | FINDINGS | job fails, findings listed |
| exit 2 with an authentication message | AUTH_ERROR | job fails immediately, no retry |
| exit 2 or 124/137 without an authentication message | INFRA_ERROR or timeout | at most one retry, then the job fails |
| exit 3 | unsupported project | job fails |
| HTTP 429 or an entitlement message | QUOTA_EXHAUSTED | job fails; it is never reported as a pass |

A quota or authentication failure on `main` fails the CI run. The release gate
requires a successful CI run for the tagged commit, so a release cannot proceed
while the security baseline is unknown. That is intentional: the gate does not
fall back to "no findings".

`scripts/ci_gate.py` accepts only these intentional skip cases: SonarCloud is
skipped on fork and Dependabot pull requests, Snyk jobs are skipped outside
`main`, and `Dependency review` is skipped on non-PR events because there is no
base commit to compare. Every other required job must succeed.

## CodeRabbit

<https://docs.coderabbit.ai/management/plans> states that open-source projects
receive Team features with no paid subscription and that OSS review limits are
1–10 reviews per developer per hour, scoped per repository. Public repositories
with fewer than 10 stars require a manual trigger.

On-demand products are billed separately and require a payment method:
CodeRabbit Security Scan, CodeRabbit Agent, usage-based reviews and Deep Scan.
This repository enables none of them. Adding one would create an unmetered cost
path, so it is out of policy.

## Sourcery and Socket

<https://www.sourcery.ai/pricing> lists the Open Source plan as free forever for
public repositories, including security checks on every pull request.
<https://socket.dev/pricing> states that Socket is and will always be free for
open source. Neither is a required check; both are advisory reviewers.

## GitHub features that must exist first

- Dependency Review reads the repository dependency graph. It is enabled for
  this repository (Dependabot runs prove the graph updates).
- CodeQL and artifact attestations need no extra purchase on a public
  repository.
- Scorecard's `Branch-Protection` check needs admin read access to report a
  score. Without a high-privilege token the check reports a lower confidence.
  This repository does not create such a token; a lower score is accepted.

## What only the maintainer can verify

The following settings are not readable from the repository API and are recorded
as manual checks:

1. Snyk organization: confirm each project's Git remote URL points at
   `https://github.com/cnkang/hermes-plugin-qdrant-memory` and that Snyk shows
   the repository as public.
2. Snyk integrations: confirm the GitHub App's automatic pull request tests are
   disabled for this repository. If they are enabled, every external pull
   request consumes a test outside Actions.
3. Snyk usage page: record the monthly test count for Snyk Code and Snyk Open
   Source, and the project count against the Free plan limit of five.
4. CodeRabbit organisation: confirm usage-based reviews, Security Scan, Agent
   and Deep Scan are all disabled.
5. GitHub organisation billing: confirm no paid runner or metered product is
   attached to this repository.

## Review cadence

Re-check this document and the linked free-tier pages at each release and after
any change to a workflow trigger. A new trigger that adds a metered call needs a
row in the trigger table before it merges.
