# Documentation Audit (2026-10-26)

Scope: Runtime, MCP distribution, and Agent Skill repositories.

## Repositories audited

- Runtime: cnkang/hermes-plugin-qdrant-memory @ 16057df (branch codex/stability-followup)
- MCP: cnkang/qdrant-memory-mcp @ d4a0218 (branch main)
- Skill: cnkang/skills @ da19b4b (branch main), qdrant-memory/

Local filesystem paths are intentionally omitted. The SHAs above identify the audited revisions.

## Key conflicts and issues found

1. README first screen led with deployment-hardening notes instead of product
   positioning. Fixed in Runtime and MCP READMEs.
2. No single "choose your setup" entry point. Users could mistake a private MCP
   profile for shared memory. Added setup tables.
3. Capabilities and limitations were scattered. Added compact matrices.
4. No unified Sharing vs Portability vs Backup explainer. Added
   docs/portability.md in Runtime. MCP and Skill link to it.
5. Harness support levels were not stated consistently. Added a harness support
   matrix to the MCP README.
6. Evidence for long-running 24-hour acceptance was not marked pending. Added
   explicit "pending / not yet verified" notes.
7. SKILL.md terminology needed a portability link. Added one.

## Claim verification

- "One Memory. Multiple Agents." tagline: adopted as recommended, no overclaim.
- Shared Owner: documented as opt-in, off by default. Preserved.
- Automatic extraction: only Hermes native and opt-in Native Owner. MCP hosts do
  not auto-extract. Preserved.
- No "zero data loss", "exactly-once in all situations", or "production-ready
  24/7" claims added.

## Unresolved / follow-ups

- Chinese READMEs: hero, setup, capabilities matrix, doc navigation, and evidence sections now match English. Remaining deep section parity (data flow, config, migration, operations) still tracked.
- Harness matrix verification levels for Codex remain "configuration example only" (no real-host evidence yet).
- Long-running 24-hour acceptance: pending / not yet verified.
- Runtime PR #95 and MCP PR #45 are both MERGED (2026-10-25). Their fault-isolation changes are in main but not yet in a released Runtime/MCP version; do not label as released until a tagged release ships.
