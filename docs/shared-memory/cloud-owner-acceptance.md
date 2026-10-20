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

Configure protected Environment secrets:

- `QDRANT_CLOUD_URL`: HTTPS database endpoint;
- `QDRANT_CLOUD_OWNER_COLLECTION`: pre-provisioned disposable collection;
- `QDRANT_CLOUD_OWNER_API_KEY`: collection-scoped database key, retained by the Owner only.

The job writes these values to a private temporary configuration file and removes
it in `always()`. It checks that the key cannot access a distinct collection. It
then validates same-agent independent devices and different-agent private
isolation. It also validates authorized project sharing, stale CAS rejection and
durable receipt replay after the Ledger reopens. Only known synthetic record IDs from that invocation are removed.
The provisioned collection and embedding marker are never deleted by the test.

This is deterministic Owner-to-Cloud integration, separate from the real HTTPS MCP
network acceptance and actual Host tests. It does not certify Host UI or model quality.
No real Cloud run has been performed for this new lane without explicit authorization.

For an authorized local run, use a mode-0600 file with `url`, `owner_collection` and
`owner_api_key` fields, then execute:

```sh
python -m pytest tests/security/test_cloud_owner.py \
  --qdrant-cloud-config=/private/path/cloud-owner-config.json -q
```
