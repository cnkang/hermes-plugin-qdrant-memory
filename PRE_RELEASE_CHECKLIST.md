# v0.1.0 Pre-release Checklist

Last reviewed: 2026-10-08. This checklist records evidence for the current hardening
branch. See [PRE_RELEASE_FINAL_REVIEW.md](PRE_RELEASE_FINAL_REVIEW.md) for details.

## Code and data safety

- **PASS** — Branch created from the latest fetched `origin/main` SHA
  `a6a883cd3db3bfdf57cb923f1e9c26fb336df211`.
- **PASS** — Existing untracked `IDEA.md` was left untouched and is excluded from
  the PR.
- **PASS** — Reproduced the stale prepared-UPSERT replay, then fixed and tested both
  same-point and cross-point/same-hash deletion cases.
- **PASS** — Scope isolation, failed DELETE retry and intentional post-delete re-add
  remain covered by regression tests.
- **PASS** — Ledger lookup indexes are added in place; a test verifies existing
  events/operations survive opening the old schema.
- **PASS** — Reviewed current scope enforcement, transcript provenance quarantine,
  profile database permissions, and sensitive backup/WAL documentation; no new
  scope bypass was found in this change.
- **PASS** — Tests and retrieval measurements use temporary synthetic data; no user
  collection was modified.

## Local validation

- **PASS** — Full test suite on Python 3.11.15 and 3.14.0: 216 passed and 3
  remote-service tests skipped on each version, with one expected local-Qdrant
  payload-index warning per run.
- **PASS** — Ruff 0.15.1 lint and formatting checks.
- **PASS** — `actionlint` over all GitHub Actions workflows.
- **PASS** — Isolated installed-distribution smoke: provider entry point loads and
  packaged `plugin.yaml` is present.
- **PASS** — Synthetic 24-query retrieval pilot completed using local Ollama and a
  temporary embedded Qdrant collection.
- **PASS** — 100,000 synthetic pending operations: indexed lookup and point/hash
  query plans verified.
- **SKIPPED** — Local remote-Qdrant tests: two Server tests require an explicit
  disposable endpoint; Cloud requires secret-gated configuration.

## Reset, host and live-service gates

- **PASS** — Existing reset tests cover process death after Qdrant deletion/before
  collection recreation and after recreation/before identity validation, plus
  recovery preserving other destinations.
- **NOT RUN** — Hard process death immediately after reset-intent commit and before
  delete; after ledger clear and before intent removal; and during a second recovery
  attempt. Additional reset errors have injected-failure coverage.
- **NOT RUN** — Latest-Hermes workflow against the sampled current `main` SHA
  `328a75e2b140000069c5775e1fdf75a1a734dddc` on Python 3.11 and 3.14.
- **NOT RUN** — Current-branch GitHub CI and the Linux/macOS/Windows compatibility
  matrix on the final PR head.
- **NOT RUN** — Current-branch authenticated Qdrant Cloud smoke.

## Release gate

- **NOT READY** — Do not tag or publish while current-head CI/latest-Hermes evidence
  is missing and the durability/retention risks documented in the final review have
  no accepted release boundary.
- **PASS** — Changes are prepared for PR review only; no merge or release action was
  taken.
