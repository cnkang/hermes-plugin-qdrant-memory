# Universal Owner implementation and acceptance

This document describes implementation boundaries and acceptance requirements.
Read the architecture decisions in [Universal cross-agent memory](universal-cross-agent-memory.md) first.
Published Runtime 0.4.0 does not include these source preview features.
Do not use historical source version strings as proof of feature support.

## Shared recall

The shared reader supplements the private Provider.
It does not create another writer to the Owner destination.
The tests cover private isolation, partial failure, provenance, time limits, and cache invalidation.
Read [Provider recall](phase-1b-provider-recall.md) for the design.

The Owner must check authorization before it delivers shared recall.
It must omit cached shared content when it cannot confirm current authorization.
The Owner cannot erase model context that it already delivered.
Do not describe revocation as removal of previously delivered context.

## Device identity

Versioned device grants preserve compatibility with legacy grants that have six fields.
The tests cover private identity, device attribution, expiry, and live revocation.
Invalid operator updates must deny access.
Agents cannot supply trusted identity through tool arguments.

## HTTPS MCP

The official SDK Streamable HTTP server receives the existing Owner.
The gateway authenticates each request and checks Host and Origin.
It uses loopback listeners and limits concurrent admission and request body size.
Administrators provide the service credentials before use.
The preview does not support OAuth interoperability.

Real TLS tests and tests across two containers provide separate evidence from in-process ASGI tests.
Cloud storage tests do not establish HTTPS MCP compatibility.
Read [Remote Owner](../shared-memory/remote-owner.md) for configuration and security boundaries.

## Automatic memory

Native Owner uses the existing Provider, Runtime, Ledger, and worker.
Its durable event API applies authorization before extraction and persistence.
A private writer with an additional shared reader alone does not provide this lifecycle.

Automatic adapters must use official host lifecycle hooks.
Unsupported hosts require explicit MCP use or assisted operation.
Adapters must not scan unrelated transcripts.
SDK tests do not establish host user interface acceptance.

## Test evidence

Record PASS, FAIL, SKIPPED, and UNVERIFIED separately.
Record the commands, source identifiers, and test scope with each result.
Keep distribution release references until a compatible Runtime release is available.
Public examples must not contain credentials or private environment details.
