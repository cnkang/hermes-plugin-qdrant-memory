# Credentialed Cloud Owner acceptance

The existing `cloud-smoke.yml` now has a separate `Scoped Cloud Owner devices and
recovery` job. It is protected by the existing `qdrant-cloud` Environment and runs
only on main/manual workflow dispatch. Fork PRs receive no Cloud credentials.
An availability job reports only a boolean. Missing scoped configuration makes
the actual Owner test job `SKIPPED` in GitHub. The job makes no Cloud request.
A successful availability job is not evidence that credentialed tests executed. If inputs
disappear after that check, preparation fails rather than creating a false pass.

Before running, an authorized operator must provision an isolated collection named
`qm_owner_ci_<unique suffix>` and an API key scoped to that collection only. The
Owner test cannot create/delete collections or use an admin key as a fallback.
Provision named dense vectors with dimensions 3 / Cosine. Enroll the embedding
identity marker of the existing Runtime for `tests.helpers.Embedder`. The
fingerprint is `test-embedding-space-v1`. Use `QdrantStore.initialize(create=True)`
in the trusted provisioning step. That step is separate from all Agent credentials.
Never provision or run this fixture against real user collections.

Initialization creates keyword indexes for the Owner filters `shared_protocol`,
`space_id`, `visibility` and `logical_agent_id`. Strict-mode backends require these
indexes. For an existing test collection, run the trusted initialization step
without reset to add missing indexes. Owner startup with `create=False` does not
create indexes. Keep strict mode and all Owner filters enabled.

Configure protected Environment secrets:

- `QDRANT_CLOUD_URL`: HTTPS database endpoint;
- `QDRANT_CLOUD_OWNER_COLLECTION`: pre-provisioned disposable collection;
- `QDRANT_CLOUD_OWNER_API_KEY`: collection-scoped database key, retained by the Owner only.

The job writes these values to a private temporary configuration file and removes
it in `always()`. It checks that the key cannot access a distinct collection. It
then validates same-agent independent devices and different-agent private
isolation. It also validates authorized project sharing, stale CAS rejection and
durable receipt replay after the Ledger reopens. Cleanup selects only the unique
Owner ID from that invocation. This includes writes with a lost response. The test
waits for deletion and checks that the exact record count for this Owner is zero.
If records remain or the check fails, the job fails. The test log reports successful
cleanup without database credentials or record contents.
The provisioned collection and embedding marker are never deleted by the test.

This is deterministic Owner-to-Cloud integration, separate from the real HTTPS MCP
network acceptance and actual Host tests. It does not certify Host UI or model quality.
Run this lane only with explicit authorization. A skipped job is not a Cloud test result.

## Disposable collection with an existing test key

An operator can explicitly authorize reuse of `QDRANT_CLOUD_API_KEY` for a manual
run. Set the workflow input `use_existing_test_key` to `true`. This option defaults
to `false` and does not apply to push events. If separate scoped inputs are present,
the workflow uses the scoped lane instead.

The manual lane creates a new `qm_owner_ci_<random UUID>` collection and enrolls
its embedding marker before it runs the same Owner isolation, CAS and recovery
contracts. It deletes the entire fresh collection after success or failure and
checks that the collection is absent. An independent `always()` step repeats this
cleanup check with the exact generated name from the private configuration file.
It does not reuse or delete an existing collection. The job removes the private
configuration file last. Do not use production credentials for this lane.

This lane does not certify collection-scoped database permissions. The separate
scoped lane retains its required HTTP 403 check. Neither lane changes Agent grants
or bypasses the Owner. A forced runner shutdown can prevent cleanup; if that occurs,
an operator must verify and remove only the invocation's disposable collection.

```sh
gh workflow run cloud-smoke.yml --ref main -f use_existing_test_key=true
```

For an authorized local run, use a mode-0600 file with `url`, `owner_collection` and
`owner_api_key` fields, then execute:

```sh
python -m pytest tests/security/test_cloud_owner.py \
  --qdrant-cloud-config=/private/path/cloud-owner-config.json -q
```
