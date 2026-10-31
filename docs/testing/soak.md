# Soak and fault-injection testing

This page documents the staged soak system that runs real Owner, Gateway,
Ledger and Qdrant paths on GitHub-hosted runners. It exists because a 24-hour
cloud soak once found writes that the client never confirmed and never replayed
([record](../validation/cross-device-cloud-soak-2026-10.md)). The fix is a retry
contract ([design](../design/remote-write-retry-adr.md)). This harness is the
regression gate that keeps it true.

The harness is staged on purpose. A short window runs first, a gate decides from
its evidence, and only then may an extended window start. A broken build
therefore costs minutes instead of runner-hours.

## Topology

Only the Owner and the harness are native processes. That keeps `SIGTERM`,
`SIGKILL`, restart and ledger recovery real.

```text
GitHub-hosted runner
├── native Python
│   ├── Qdrant Memory Owner (remote-serve, loopback HTTP + shared service)
│   ├── soak client (product remote MCP client, durable outbox)
│   └── deterministic embedding and extraction providers (loopback)
└── Docker
    ├── Qdrant server (pinned digest, API key)
    └── toxiproxy (network scenario only, pinned digest)

TLS
  soak client --HTTPS--> nginx --HTTP--> Owner (127.0.0.1)
  Owner --HTTPS--> nginx --HTTP--> Qdrant (Docker)
```

The client reaches the Owner through the same HTTPS proxy policy the deployment
documents. That policy fixes `Host`, rejects forwarded headers, bounds bodies and
uses a short header timeout. Both TLS listeners use a per-run CA, so a run never
trusts a shared certificate.

## Modes

| Mode | Stages | Window per leg |
| --- | --- | --- |
| `quick` | preflight, smoke matrix, gates, final report | 480 s |
| `full` | quick stages plus the extended matrix | 18000 s (5 h) |

`full` runs the smoke matrix first. The extended matrix starts only when the
smoke gate is green and, when requested, the cloud window passed.

Triggers are `workflow_dispatch` and a weekly schedule (`0 3 * * 0`), which runs
`full` on `main`. There is no `push` or `pull_request` trigger, so no untrusted
change can reach a cloud credential.

## The six legs

Both architectures run every scenario:

| Scenario | Covers | Faults |
| --- | --- | --- |
| `steady` | add, search, exact dedupe, update, delete, CAS races, capture events, private and project isolation, growing collection | none |
| `restart` | `SIGTERM`, `SIGKILL`, in-flight requests, receipt replay, recovery and WAL, lease re-acquisition | one restart every 45 s (quick) or 120 s (full), every third one forced |
| `network` | connection reset, read timeout, latency, short disconnect, lost responses, `Retry-After` | toxiproxy on Owner → Qdrant and client → Owner, plus abandoned responses |

Every leg uses eleven provisioned credentials. Seven are project writers (five
drive the workload and two own the compare-and-swap probe), two are read-only
and two own private partitions. Each device carries its own token,
and the Owner stores only token hashes. Private partitions share a logical agent
identity in pairs, so isolation is tested where it must hold and sharing is
tested where it must work.

## Invariants

The verifier compares the acknowledged journal with the ledger rows and with the
exact Qdrant points. A search hit is never used as proof of a write.

| Invariant | Meaning |
| --- | --- |
| `write_durability` | every acknowledged write exists with its acknowledged revision, and nothing deleted comes back |
| `request_id_idempotency` | one receipt and one record per request identifier |
| `event_id_idempotency` | one receipt and one memory per lifecycle event |
| `authorization` | a read-only credential cannot write |
| `scope_isolation` | no private record is visible to another credential |
| `ledger_integrity` | `integrity_check` passes with no pending, failed or open rows |
| `no_unexplained_loss` | no acknowledged write is missing and no failure is unexplained |
| `writer_lease` | exactly one Owner process per window, with a successful start for every restart |
| `harness_clean` | the harness itself raised no unhandled exception |
| `window_complete` | the measured window covered at least 95 percent of the target |
| `retrieval_quality` | retrieval is measured, separately from loss |
| `workload_volume` | the window actually produced acknowledged work |
| `probe_results` | every injected probe reported a verified result |
| `scenario_coverage` | the scenario actually injected the faults it claims to test |

A run may end in `PASS`, `FAIL`, `INCOMPLETE` or `HARNESS_ERROR`. `skipped`,
`cancelled`, missing artifacts and missing evidence are never translated into
`PASS`.

## Evidence

Every leg writes a durable JSONL stream before it writes anything else, so a
crash cannot delete the window that produced it.

| File | Content |
| --- | --- |
| `events.jsonl` | one record per attempt and per logical operation, plus control and fault boundaries |
| `report.json` | identity, status, invariant results, counts, latency, ledger status, trends |
| `metrics.json` | resource and storage samples for the window |
| `junit.xml` | one test case per invariant, so the runner records each result |
| `owner.log` | scrubbed Owner log, truncated to the last 8 MiB |
| `ledger/` | read-only copy of `state.db` and its sidecars |

The memory text in the journal is generated by the harness for this run. No
user content and no production memory enters a soak artifact. Artifacts never
contain a token, an API key or a full ledger dump beyond the read-only copy. The scrubber removes registered credentials and explicit
`Bearer`/`api_key=` forms. Commit hashes and content hashes stay readable, because
evidence that cannot be checked is not evidence.

## GitHub environments

Two environments hold the whole configuration. Do not reuse `qdrant-cloud`,
`model-quality` or `model-smoke`. Those belong to other acceptance paths.

### `soak-local`

| Setting | Value |
| --- | --- |
| Branches | `main` only |
| Secrets | none |
| Variables | none |
| Reviewers | none |
| Workflow usage | `environment: {name: soak-local, deployment: false}` |

`deployment: false` is safe here: the environment has no protection rule that
depends on a deployment object, and the job only needs the environment name.

### `soak-cloud`

| Setting | Name | Kind |
| --- | --- | --- |
| Required | `SOAK_QDRANT_URL` | variable |
| Required | `SOAK_QDRANT_COLLECTION` | variable |
| Required | `SOAK_QDRANT_API_KEY` | secret |
| Optional | `SOAK_LLM_BASE_URL` | variable |
| Optional | `SOAK_LLM_MODEL` | variable |
| Optional | `SOAK_LLM_API_KEY` | secret |

Branches: `main` only. Reviewers: keep the required reviewer. The workflow does
**not** set `deployment: false` for this environment, because an approval must
be attached to a deployment object. A cloud job therefore waits for a human
before it starts, which is the intended behaviour.

Rules that the workflow enforces:

- A cloud window runs only when `include_cloud` is true, the smoke gate passed,
  the ref is `refs/heads/main` and the repository is the upstream one.
- When `include_cloud` is true but a *required* name is missing, the job fails
  with the exact missing names and a link to the settings page. It never skips.
- The optional LLM names enable lifecycle capture against a real model. When
  they are absent, the cloud window exercises the tool surface with the
  deterministic local providers and says so in the report.
- The secret values are read only inside the cloud job, in step-level `env`.
  No other job references `vars.SOAK_*` or `secrets.SOAK_*`.

### Provisioning the environments

A maintainer must create and configure both environments in repository settings
or via the API before running the soak. The workflow does not configure
protection rules or environment secrets. If a job references a missing
environment, GitHub Actions creates it automatically without protection rules
or secrets. The commands below configure the `main` branch policy for both.
Before requesting a cloud window, ensure `soak-cloud` has the `main` branch
policy and a required reviewer.

```bash
gh api -X PUT repos/<owner>/<repo>/environments/soak-local \
  -F 'deployment_branch_policy[protected_branches]=false' \
  -F 'deployment_branch_policy[custom_branch_policies]=true'
gh api -X PUT repos/<owner>/<repo>/environments/soak-cloud \
  -F 'reviewers[][type]=User' -F 'reviewers[][id]=<user-id>' \
  -F 'deployment_branch_policy[protected_branches]=false' \
  -F 'deployment_branch_policy[custom_branch_policies]=true'
gh api -X POST repos/<owner>/<repo>/environments/soak-local/deployment-branch-policies \
  -f name=main
gh api -X POST repos/<owner>/<repo>/environments/soak-cloud/deployment-branch-policies \
  -f name=main
gh variable set SOAK_QDRANT_URL --env soak-cloud --body 'https://<cluster>'
gh variable set SOAK_QDRANT_COLLECTION --env soak-cloud --body '<collection>'
gh variable set SOAK_LLM_BASE_URL --env soak-cloud --body 'https://<llm-endpoint>'
gh variable set SOAK_LLM_MODEL --env soak-cloud --body '<model>'
gh secret set SOAK_QDRANT_API_KEY --env soak-cloud
gh secret set SOAK_LLM_API_KEY --env soak-cloud
```

`SOAK_LLM_BASE_URL` and `SOAK_LLM_MODEL` are non-sensitive configuration, so set
them with `gh variable set`. Keep API keys and other sensitive LLM credentials
as secrets with `gh secret set`, which prompts for the value and never
prints it.
Verify with `gh api
repos/<owner>/<repo>/environments/<name>` and the `variables` and `secrets`
sub-resources, which return names and metadata only.

### Fixed collection and cleanup

The cloud window writes into the fixed test collection from
`SOAK_QDRANT_COLLECTION`. Each run stamps its records with a run-scoped
`owner_id` derived from its own run identity, and cleanup deletes exactly
those records:

1. count the records of this run's scope.
2. delete them by filter.
3. count again and require zero.
4. require that the collection itself still exists.

A separate workflow step re-reads the collection and proves two things: no
record of this run remains, and the collection itself is still there. A cleanup
that cannot prove zero remaining records marks the run `CLEANUP_FAILED`. The
collection is never deleted.

The embedding fingerprint includes the provider base URL, so the cloud job pins
the local provider port (`18431`). Without a stable port a fixed collection
would look like a different pipeline on every run.

## Running locally

Requirements: Docker, `nginx` with the SSL module, `openssl`, and the project
installed with the `remote` extra.

```bash
python -m scripts.soak.run \
  --scenario steady --profile quick --target-seconds 120 --workers 3 \
  --workdir /path/outside/the/repo/work \
  --report-dir /path/outside/the/repo/report \
  --platform-label local
```

The work directory must not sit under a symlinked parent. Protected mode refuses
such a path, which matters on macOS where `/var` and `/tmp` are links.

Local notes:

- macOS: install nginx with `brew install nginx`, and use a work directory under
  your home, never `$TMPDIR`.
- Docker Desktop reaches the host through `host.docker.internal`, so the network
  scenario maps toxiproxy ports. On Linux the harness uses host networking
  instead, which keeps every listener on loopback.
- A run owns its Docker containers by name (`soak-qdrant-<seed>`) and removes
  them at teardown. Two concurrent runs on one machine need distinct seeds.

## Running on GitHub

```bash
gh workflow run soak.yml --ref main -f mode=quick -f include_cloud=false
gh workflow run soak.yml --ref main -f mode=full  -f include_cloud=false
gh workflow run soak.yml --ref main -f mode=quick -f include_cloud=true -f cloud_seconds=180
```

`workflow_dispatch` requires the workflow file on the default branch, and both
environments allow only `main`. A run from a feature branch is therefore blocked
by design. Merge first, then dispatch.

Cost envelope: a `quick` run uses six runners for about 12 minutes each. A `full`
run uses six runners for up to 350 minutes each, roughly 35 runner-hours.

## Reading a result

The final job prints a table per stage and fails unless every expected leg is
`PASS`. Useful first stops:

| Symptom | Meaning |
| --- | --- |
| `INCOMPLETE` with `missing artifact` | a leg was skipped, cancelled or died before it wrote evidence |
| `INCOMPLETE` with `window ... cannot support` | the window was shorter than the acceptance requires |
| `HARNESS_ERROR` | the harness failed, so the product status is unknown and never a pass |
| `CLEANUP_FAILED` | this run's cloud records were not provably removed |
| `unresolved` above zero | a write never received an acknowledgement, even after the recovery drain |
| `replicas`/`duplicate_records` above zero | a replay created a second logical record |
| `pending` or `quarantined` above zero | the ledger still holds work that needs operator attention |

Long `p99` latency in a `network` leg is expected: the client honours
`Retry-After`. `latency_clean_p50_p95_p99` excludes operations that needed a
replay, so a throttle stall cannot hide the ordinary path. `expected_path_outages`
counts handshake resets that happened inside a declared client-path fault window,
where the request never reached the Owner.

## Limits

- Six parallel five-hour legs are not one Owner running 24 hours. The record
  says which one was measured.
- Embeddings and extraction are deterministic local providers. The harness
  measures durability, isolation and restart behaviour, not model quality.
- One Owner per destination is unchanged. The harness never starts a second
  Owner against the same collection to imitate another device.
- The extended matrix is a regression gate, not a capacity benchmark. Throughput
  numbers describe one loopback deployment on a hosted runner.
