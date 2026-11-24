# Annotation review record

Reviewer: Codex, explicitly delegated by the user on 2026-11-16. These are
invented scenarios, not real user preferences or operational instructions.
This is agent review, not independent human review.

Every question was checked against its own group's memory text before running
retrieval. Current, historical and task-specific questions are separate intents,
not repeated paraphrases. We checked time qualifiers, negations, entity identity,
scope visibility, direct versus contextual evidence, deleted records and
multi-evidence requirements. The resolved per-question audit IDs are recorded
in review.json; rationale and all evidence IDs are in annotations.jsonl.

Corrections before freezing: the language group's default/exception questions
were separated so the current query actually distinguishes the old English
default from the current Chinese default. Other-project distractors were removed
from current stale_fact conflict labels: they remain competitive corpus items,
but are not counted as old facts of the same entity. Historical questions retain
the earlier fact as a positive. Same-value historical evidence is contextual,
not a false conflict (encryption-key). Answers inferred from two records are
both required (meeting-exception, release-checklist and multi_answer groups).

Known limitations fixed before results: only two holdout no-answer groups and
three no-answer questions; the preregistered ten-group no-answer condition cannot
be established by this first batch. Small English/code strata may also produce
wide intervals. These limits prevent GO when other criteria pass; they do not
relax any criterion. Scope/deletion lifecycle has deterministic coverage beyond
the semantic dataset. The dataset evaluates retrieval evidence, not whether a
language model will produce the correct final answer. Linguistic tags may overlap
with scenario and code tags; all reported group counts are explicit.

No optimizer or retrieval result was consulted during this review. The fixed
settings reuse the existing PR rather than selecting parameters from these data.
Further retrieval changes after holdout exposure require a new holdout.
