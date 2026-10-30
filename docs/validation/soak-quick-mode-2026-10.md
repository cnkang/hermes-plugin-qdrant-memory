# Staged soak system and quick-mode acceptance (2026-10)

This record covers the new staged soak system: what it runs, what it proved on a
local runner, and which parts are still blocked. The 24-hour cloud soak record
([cross-device soak](cross-device-cloud-soak-2026-10.md)) and the retry-contract
record ([restart boundary](restart-boundary-retry-2026-10.md)) list the defects
this gate exists to catch.

## 1. What was added

| Item | Path |
| --- | --- |
| Harness (prepare, supervisor, load, verify, aggregate, cloud audit) | `scripts/soak/` |
| Harness and workflow contract tests | `tests/soak/` |
| Staged workflow with two environments | `.github/workflows/soak.yml` |
| Operating and configuration guide | [testing/soak.md](../testing/soak.md) |

No product code, collection identity, tool schema, credential format or
persistence format changed. The harness drives the real Owner, Gateway, Ledger,
remote client and a real Qdrant server.

## 2. Control flow

`preflight` validates the workflows with a checksum-pinned actionlint and runs
the harness contract tests. Six smoke legs (`ubuntu-24.04` and `ubuntu-24.04-arm`
× `steady`, `restart`, `network`) run for 480 seconds each. `local-smoke-gate`
aggregates their artifacts and requires all six. Only then may `admission-gate`
schedule the five-hour matrix, and only in `full` mode. `final-report` runs with
`if: always()` and fails unless every expected leg is `PASS`.

`soak-local` carries no credentials and uses `deployment: false`.
`soak-cloud` keeps its required reviewer, so the cloud job creates a deployment
and waits for approval. A cloud job never runs from a feature branch, because
the environment allows only `main` and the preflight refuses the request.

## 3. Environment audit (GitHub API, 2026-10-30)

| Environment | Branches | Reviewers | Variables | Secrets |
| --- | --- | --- | --- | --- |
| `soak-local` | `main` | none | none | none |
| `soak-cloud` | `main` | `cnkang` | `SOAK_LLM_BASE_URL`, `SOAK_LLM_MODEL`, `SOAK_QDRANT_COLLECTION`, `SOAK_QDRANT_URL` | `SOAK_LLM_API_KEY`, `SOAK_QDRANT_API_KEY` |

Secret values were never read. Only names and metadata are recorded here. The
workflow checks three required names in one step, and fails with the exact
missing names when a cloud window is requested without them.

## 4. Static verification

| Check | Command | Result |
| --- | --- | --- |
| Workflow lint | `actionlint` 1.7.12 | PASS, no findings |
| Harness and workflow contract tests | `pytest tests/soak -q` | PASS, 74 passed |
| Full repository suite with a Qdrant container | canonical host runner, `-j 1` | PASS, 99 files, 1216 passed, 0 failed, 11 gated skips |
| Lint and format | `ruff check` and `ruff format --check` | PASS |
| Documentation links | link checker over tracked markdown | PASS, no missing local links |

The contract tests execute the workflow's own cloud precondition step twice:
once with every required name and once without them. "Missing configuration
fails, not skips" is therefore a tested behaviour rather than a claim.

## 5. Local quick-profile window

One ARM64 machine ran the three scenarios at the quick-profile window on the
pull request head `7fe75c1`. Each leg requested 480 seconds and measured at least
95 percent of that window. Qdrant
1.15.5 ran from the repository's pinned digest, nginx 1.31.8 terminated both TLS
listeners, and MCP SDK 1.30.0 carried the client.

| Leg | Status | Window (s) | Requests | Acknowledged writes | Events | Losses | Duplicates | Restarts |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `steady` | PASS | 489.9 | 1834 | 1237 | 152 | 0 | 0 | 0 |
| `restart` | PASS | 488.6 | 1821 | 1228 | 152 | 0 | 0 | 10 |
| `network` | PASS | 490.6 | 1900 | 1220 | 150 | 0 | 0 | 0 |

Every leg passed all fourteen hard invariants. One of them, `scenario_coverage`,
requires a restart leg to have restarted and a network leg to have closed fault
windows. The restart leg forced a `SIGKILL`
on every third restart. Seven graceful and three forced restarts each ended in a
fresh receipt and a clean ledger. The network leg applied 24 fault windows across
both sides of the Owner. It also dropped one response on purpose and recovered it
under the same request identifier, and recorded one handshake outage that never
reached the Owner.

| Measure | `steady` | `restart` | `network` |
| --- | --- | --- | --- |
| Clean latency p50 / p95 / p99 (ms) | 34.7 / 48.4 / 74.7 | 35.2 / 49.9 / 77.0 | 53.3 / 74.2 / 104.5 |
| Transient attempts / retried operations | 28 / 25 | 24 / 24 | 32 / 30 |
| Probe rounds (all PASS) | 32 | 32 | 50 |
| Retrieval hit rate | 0.984 | 0.997 | 0.990 |
| Ledger integrity | ok, no pending | ok, no pending | ok, no pending |
| Owner RSS change over the window | -6.2 MB | +3.5 MB | -22.2 MB |
| Owner descriptor change over the window | 0 | +1.4 | -0.1 |

The aggregate over the three artifact directories reported `PASS` with three of
three expected legs. A duplicate record, an acknowledged write without its
revision, an open receipt or a failing probe would each have failed it.

## 6. Not verified

These items were not run. They are not PASS results.

- GitHub-hosted quick mode. Both environments allow only `main`, and
  `workflow_dispatch` requires the workflow on the default branch. The workflow
  is not on `main` yet, so no hosted run exists. Status: BLOCKED.
- GitHub-hosted cloud quick mode. Same branch restriction plus the required
  reviewer. Status: BLOCKED.
- The five-hour `full` matrix. Status: NOT RUN.
- x64 and ARM64 hosted runners. Only a local ARM64 machine ran the harness.
  Status: NOT VERIFIED for hosted runners.

## 7. Residual risk

- A caller that ignores `retryable` still drops an admitted write. The harness
  can only prove that the product classifies and replays correctly.
- The deterministic local providers exercise durability, isolation and restart
  semantics, not model quality or extraction accuracy.
- The harness treats a TLS handshake reset inside a declared client-path fault
  window as an expected outage. Outside such a window the same answer is a
  failure. The distinction rests on the recorded fault boundaries.
- The fixed cloud collection depends on a stable embedding fingerprint. The
  cloud job pins the local provider port for that reason. Changing the port,
  model or dimensions requires a new collection.

## 8. What the next run must show

After the pull request merges, dispatch quick mode on `main` and confirm six
hosted legs plus the final report. Then run the five-hour matrix before treating
the extended window as accepted. The exact commands are in
[testing/soak.md](../testing/soak.md#running-on-github).
