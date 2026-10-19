# Shared destination hardening plan

The opt-in destination policy binds the existing canonical backend, collection,
embedding fingerprint and schema to one profile. A durable local claim and OS
lock cover cooperating Runtime entry points under the same operating-system
account. Database credentials and network isolation remain separate controls.
The shared protocol, memory payloads and existing default behavior stay unchanged.

- Add negative tests before implementation for destination mismatches, unsafe
  files, conflicting profiles, sanitized diagnostics and non-mutating failures.
- Add strict optional destination policy validation and local destination claims
  at the existing WriterLease boundary. Validate before recovery or storage I/O.
- Add a read-only shared-doctor command with stable codes and explicit UNVERIFIED
  outcomes for controls that cannot be established from local evidence.
- Exercise existing authorization, revision, replay, recovery, standalone and
  Hermes tests. Keep live database authorization checks separate from ordinary CI.
- Document threat boundaries, four deployment routes, coherent backup and
  recovery, validation results and bilingual proposed release notes.
- Update MCP and canonical Skill guidance without changing published artifacts
  or dependency pins before a Runtime release exists.
- Review tracked changes for private details, validate packaging and formatting,
  commit code and documentation separately, then push review branches and PRs.

Cross-container client transport is deferred: the current private API remains
bound to loopback. No tags, package publication or release creation are included.
