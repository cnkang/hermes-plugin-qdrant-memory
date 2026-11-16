# Freeze provenance

The dataset, individual annotations, retrieval settings and decision boundaries
were frozen before any holdout retrieval. `review.json` lists every reviewed
question. During development, a no-answer scoring edge case (empty positive
set with a returned conflict) was fixed and tested before holdout execution.
The real model digest was recorded and enforced before holdout execution.

The first full local run used freeze SHA256 recorded in its raw JSON. After that
run began, Python docstrings and formatting were completed to satisfy the
repository lint rules; these do not change dataset, settings or scoring.
Before GitHub execution, source hashes were refreshed to those documented
sources. The runner additionally validates expanded annotations against their
authoring source and attributes a supplied failed lifecycle report as a safety
failure. These validation changes do not change successful rankings or metrics.
No tokenizer, RRF, candidates, thresholds, labels or decision boundary changed
after holdout exposure. A future retrieval variant requires new holdout groups.
