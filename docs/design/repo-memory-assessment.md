# Repository memory assessment

**Status:** DESIGN ONLY. No repository-memory product is implemented. This
document records the identity boundary, a minimum skill-level proposal, the
maintenance cost and the test plan.

## Candidate workflow

A lightweight workflow that keeps repository knowledge in the existing memory
backend:

- Architecture notes and module responsibility.
- Important entry points and ownership boundaries.
- Historical design decisions and their reasons.
- Verified engineering lessons.
- Reusable repository procedures.

## What exists today

| Capability | Reality | Location |
|---|---|---|
| Memory partition | `Scope(user_id, agent_id)`. A session is a payload field, not a partition | `qdrant_memory/models.py` |
| Project sharing | Opt-in shared Owner grant bound to `(owner_id, space_id)`. The scope derives from the grant | `qdrant_memory/shared_service.py` |
| Capture rules | Native Owner captures to a project only through an approved grant rule and a profile allowlist | `qdrant_memory/native_owner.py` |
| Repository identity | Not modelled. No working directory, repository name or remote URL participates in an access check | Absent |
| Code indexing | Not present. No repository crawler, syntax index or git watcher | Absent |

## Identity boundary

Repository identity, workspace identity and authorization stay separate. A path,
a name or a remote URL proves location. It does not prove permission.

Any future repository memory must therefore:

- Resolve the target space from an operator grant, not from the working directory.
- Refuse an operation when no grant covers the repository.
- Keep private profiles private, even when two repositories share a name.
- Never accept a user-supplied project identifier as authorization.

## Minimum implementation proposal

1. Skill-level procedure: record architecture notes and decisions with the
   existing tools. Name the repository and the component in the text.
2. Use an existing shared space for team-wide repository knowledge. The grant
   supplies identity and visibility.
3. Freshness check: store the commit or version that the note describes in the
   text. Recheck it before reuse.
4. No second database, no second embedding pipeline and no background git
   service.

## Maintenance cost

| Option | Cost | Risk |
|---|---|---|
| Skill guidance only | Documentation only | A note goes stale when it names no commit |
| Scheduled repository re-index | High: a new pipeline, storage and review | Duplicate knowledge and drift |
| Background git watcher | High: a service to run and secure | Unauthorized capture and surprise writes |

## Security risks

- Treating a working directory as an authorization signal.
- Copying a note from one repository space into another.
- Indexing files that contain secrets.
- Letting a repository note grant a permission or a release approval.

## Test plan

- Scope test: a grant for repository A must not read repository B.
- Freshness test: a note that names an old commit is flagged before reuse.
- Injection test: a note that contains instructions stays data.
- Volume test: a bounded number of records per repository workflow.

## Decision

Implement the skill-level procedure first. Defer any indexing service until a
measured need exists.
