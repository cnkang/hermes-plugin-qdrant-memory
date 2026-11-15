# Upgrade guide for published Runtime 0.7.1

This page describes the published Runtime 0.7.1 wheel. The
[main-branch guide](upgrade.md) also documents an unreleased local Ledger write
watermark that is not part of the 0.7.1 wheel.

Runtime 0.7.1 preserves the recovery record of an interrupted upgrade and its
backup reference. It also blocks a policy apply while the Shared Owner still has
an admitted receipt whose event is not terminal. It validates configuration,
collection, destination, point count and embedding identity, but it does **not**
record a local Ledger write watermark.

## Keep the stop-write window closed

From the moment the backup is created until `upgrade resume` completes, keep
every Hermes gateway, provider worker and MCP server that writes this profile
stopped. For a remote destination, stop every other Owner, device and credential
that can write the same collection.

If any writer resumed, or you cannot confirm that the stop-write window stayed
closed, do not run `upgrade resume`. This applies even when the point count and
embedding fingerprint look unchanged and `upgrade status` reports the plan as
valid. Runtime 0.7.1 cannot detect an in-place payload update by itself.

Stop every writer again, then run:

```sh
qdrant-memory --home /path/to/profile upgrade plan --replace
```

The replacement archives the old plan and its backup reference before saving a
new plan. Take a new backup at the new stopped-writer point and continue with
`upgrade apply --plan <new_plan_id>`. Do not treat the old backup as a new
checkpoint, and do not edit or delete it.

Read-only `upgrade status` access does not change the destination. A successful
resume is not evidence that every payload stayed unchanged. For Cloud or Server
destinations, take the new backup through the provider at the new boundary and
keep the existing isolation and operator-backup confirmations.
