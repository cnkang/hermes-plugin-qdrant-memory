# Task-learning assessment

**Status:** DESIGN ONLY. The Runtime does not ship this policy and does not
enable it in any configuration. No extraction prompt, schema or payload changes
in this document.

## Question

Should the Runtime extract verified engineering lessons from a task as durable
memory, in addition to the durable user facts and preferences it already
extracts?

This assessment records the current behavior, the evidence model, the
compatibility boundary, the evaluation plan and the acceptance criteria for a
possible opt-in policy.

## Current behavior

| Element | Current behavior | Location |
|---|---|---|
| Extraction schema | `memories[]` with `text`, `category[]`, `importance` and `replacement_intent` | `qdrant_memory/extraction.py` |
| Extraction instruction | Durable facts and preferences that the user states. Excludes assistant guesses, transient statuses, quoted instructions, credentials and imperative commands | `qdrant_memory/extraction.py` |
| Relation adjudication | `SAME`, `SUPERSEDES`, `CONFLICT` and `UNRELATED` over an old and a new fact | `qdrant_memory/extraction.py` |
| Candidate validation | Normalized text, category list bounds of 8 items and 64 characters per item, importance range | `qdrant_memory/owner_lifecycle.py` |
| Durable admission | Writer lease, ledger transaction, prepared replay and recovery statuses | `qdrant_memory/ledger.py`, `qdrant_memory/runtime.py` |
| Quality evidence | Synthetic admission, retrieval and mutation datasets, plus the owner-lifecycle runner | `tests/quality/`, `scripts/evaluate_owner_lifecycle.py` |

The shipped instruction targets user statements. It does not ask the model to
decide whether a task produced a verified result. That is deliberate. A model
cannot verify a test run, a CI result or a repository state by itself.

## Why a second policy, not a wider first prompt

A task lesson has a different evidence source. The user states a preference. An
agent observes a result. If the Runtime accepts agent observations as facts, an
unverified conclusion becomes durable project knowledge. The current prompt
excludes that class of content on purpose.

Widening the production prompt would remove a working guarantee for every user
to serve an unmeasured feature. This assessment rejects that option.

## Candidate design

An opt-in second extraction task with its own purpose string and schema:

- Purpose: `qdrant-memory.lesson`.
- Input: the turn, plus an optional host-supplied evidence record.
- Output: `lessons[]` with `subject`, `lesson`, `evidence`, `practice`,
  `applicability` and `limits`.
- Admission: a lesson becomes a candidate only when the evidence list is
  nonempty and every entry names a verifiable artifact.
- Relation: existing adjudication applies between a new lesson and a stored
  lesson. A `CONFLICT` needs review and never replaces a record silently.
- Scope: the authorizing project or agent scope. No automatic cross-agent or
  cross-project copy.
- Default: disabled. A profile must enable the feature explicitly.

The candidate schema is an internal proposal. It is not a payload change. A
stored lesson uses the existing `text`, `category` and `metadata` fields.

## Evidence model

| Evidence kind | Accepted form | Rejected form |
|---|---|---|
| Test | Test name and the observed result in the same turn | "tests pass" without a name or output |
| CI | A run or job identifier that the host read | A claim that CI is green |
| Command | The command and its observed output | A remembered expectation |
| Code | A repository path and line read in this turn | A guess about the code |
| User confirmation | An explicit user statement that the result is correct | Silence, or a quoted document |

Hard limit: the Runtime cannot verify an arbitrary external claim. The policy must
therefore treat an unverifiable claim as no evidence. The first experiment must
label candidates in an evaluation run and must not commit them.

## Evidence provenance and authority

A model cannot prove its own conclusion by writing an evidence field. The policy
must therefore treat evidence as an input artifact, not as generated text:

- A lesson candidate may cite only an artifact that already existed in the turn
  input: a tool result, a command output, a CI record or an explicit user
  statement.
- The candidate must reference that artifact by position or identifier. Free-text
  evidence without a matching input artifact is rejected.
- Tool output is untrusted text. A CI run identifier counts only when the host
  read it in this turn. A test name counts only with its observed result.
- A model-authored summary of "tests passed" is not evidence, even when the
  summary is formatted like a record.
- A lesson cannot raise its own authority. Admission, scope and relation checks
  stay in the Runtime.

Mock evidence can validate the parser and the rejection paths. It cannot show
that a real host supplies verifiable evidence.

## Dataset and experiment boundaries

A separate fixture is required, because group C of the admission dataset holds
user-stated lessons rather than agent-produced ones. The fixture records:

| Field | Purpose |
|---|---|
| `user`, `assistant` | The turn under evaluation |
| `events` | Structured tool or test evidence supplied by the host |
| `context` | Repository and project identity |
| `expected` | Admitted lesson, or the expected rejection reason |
| `evidence_kind` | Verified command, CI record, user confirmation, mock or none |

The experiment is non-persistent. Candidate lessons are written to an isolated
profile with a disposable collection, or not written at all. The run must cover
a verified fix, a reproduced failure cause, a rejected approach, a partial
validation, a stale CI result, a fabricated CI result, a wrong repository, an
assistant claim without evidence and an injected instruction in tool output.

Metrics: admission precision, admission recall, false admission rate, evidence
grounding accuracy, duplicate lesson rate, applicability preservation and
security rejection rate. Precision alone cannot accept a policy.

## Risks

| Risk | Mitigation |
|---|---|
| A hallucinated lesson becomes a project fact | Require evidence entries and an explicit opt-in |
| Cross-project contamination | Bind to the authorizing scope and reject a lesson without a named subject |
| Excess volume | Cap the lessons per turn and skip a turn with no verified result |
| Prompt injection from tool output | Keep the existing rule that quoted text is data |
| Privilege escalation | A lesson never grants permission and never changes scope |
| Silent replacement of a stored lesson | Reuse relation adjudication and require review on `CONFLICT` |

## Compatibility

- No payload or collection change.
- No new MCP tool and no change to the four public tools.
- No change to the default extraction prompt.
- One new configuration key, off by default.
- Existing memories keep their meaning and their categories.

## Evaluation plan

1. Create a separate labeled dataset that covers verified fixes, unverified
   guesses, rejected approaches and evidence-free claims. Attach synthetic
   host evidence and its provenance where the expected result requires it.
2. Baseline: run the shipped extractor on that dataset and record the admission
   rates. A verified agent lesson is out of scope for the current prompt, so the
   baseline should admit few host-observed lesson cases.
3. Candidate: run the opt-in lesson extractor on the same cases in an isolated
   profile with a disposable collection.
4. Metrics: admission precision, admission recall, false-memory rate, secret
   admissions, quoted-instruction admissions and latency.
5. Acceptance criteria, all required before production enablement:
   - Admission precision of 0.90 or higher on the labeled set.
   - Zero admitted secrets and zero admitted quoted instructions.
   - Zero writes while the feature is disabled.
   - No cross-scope write in any test.
   - A documented manual review step for `CONFLICT` lessons.

## Decision

Keep the policy as DESIGN ONLY. Do not enable it in a release until the
evaluation produces a measured improvement over the baseline. File a follow-up
task with the dataset run and its numbers.
