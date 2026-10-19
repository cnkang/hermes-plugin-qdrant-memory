# Universal Owner implementation and acceptance ledger

Date: 2026-10-19. Requirements: the supplied universal cross-agent/cross-device
development prompt, all sections 1–14. This is an implementation ledger, not a
replacement for the accepted Phase 0 ADR.

## Baseline and dependencies

- Runtime main `68a9ed1`; released package 0.4.0 predates destination hardening.
- Open Runtime PR #63, `feat/shared-reader`, head `281ab77`: reuse its reader;
  do not duplicate it or merge the PR. Local `design/phase-1b` also contains a
  Phase 1b design. Preserve that branch; continue on `codex/universal-owner`.
- MCP main `60d0cda`, source 0.2.2, GitHub release is draft.
- Skill main `d6062f0`, skill 0.3.1. No other open PRs at audit time.
- Local tests initially failed collection without the real Hermes checkout on
  PYTHONPATH. This is an environment failure, not a passing baseline.

## Work order and acceptance

1. Phase 1b: tests first in `tests/test_shared_recall.py`; opt-in validated
   configuration in `shared_recall.py`; wire read-only recall into `provider.py`.
   Keep private extraction durable and default output unchanged. No second
   writer to an Owner destination. Test private isolation, partial failure,
   provenance, time bounds, cache invalidation and no shared writes.
2. Phase 3 prerequisite: tests first for versioned device grants; compatibility
   reading of six-field grants; private identity consistency, per-device
   attribution, expiry and live revocation. Operator updates must fail closed.
3. Phase 2a: official SDK Streamable HTTP factory receives an Owner only.
   Authentication per request, no trusted identity arguments, strict Host/Origin,
   loopback bind, bounded admission and body size. Static provisioned credentials
   only; OAuth interoperability remains unsupported until separately tested.
   Real TLS and two-container tests are distinct from in-process ASGI tests.
4. Phase 4: inspect public host hooks before choosing an adapter. Keep unsupported
   hosts explicit MCP only. No invented lifecycle and no transcript crawling.
5. Phase 5: integrate tests into existing workflows, provide TLS/proxy deployment,
   cross-repository examples and Chinese evidence report. Never equate SDK tests
   with Host UI acceptance or Cloud storage tests with HTTPS MCP acceptance.

## Delivery rules

Each implementation unit gets a separate commit after its regressions pass.
Keep current release pins until a compatible Runtime is actually published.
No release, merge, production configuration, production secrets or migration.
Record PASS/FAIL/SKIPPED/UNVERIFIED with commands and actual coverage.

## Unresolved acceptance risks

The Phase 1b design permits bounded cache staleness, whereas the task requires
deleted/revoked shared memories not to be reinjected. Shared recall therefore
needs fresh authorization at delivery, or must omit shared cached content.
Already delivered model context cannot be remotely erased. A new implementation
must state this boundary and cannot advertise immediate context revocation.

The Native private writer plus an additive shared reader covers two distinct
destinations. It does not by itself implement Native lifecycle writes through
one Owner. That requires a durable Owner event API and host adapter, and must be
tracked separately from read-only Phase 1b.
