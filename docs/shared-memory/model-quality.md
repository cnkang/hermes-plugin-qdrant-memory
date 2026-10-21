# Optional real-model lifecycle quality

[English](model-quality.md) | [简体中文](model-quality.zh-CN.md)

## Evaluation boundary

This source preview evaluates the existing Owner Runtime through actual Hermes MemoryManager callbacks, Pi SDK sessions, and official HTTPS MCP clients.
The Owner Runtime controls memory extraction and writes. The evaluation uses its existing extraction and write paths.
MCP means Model Context Protocol. SDK means software development kit.

The Pi assistant gives a fixed acknowledgement for each turn.
Extraction and embeddings use the selected model profile.
An embedding represents text as a numeric vector.
The `reference` profile uses installed `qwen3:8b` and `qwen3-embedding:4b` models through Ollama on loopback HTTP.
This profile needs no API key, Cloud service, or model download.

## Frozen baseline and quality gate

The frozen anonymous corpus is [lifecycle-v1.json](../../tests/quality/lifecycle-v1.json).
It contains 24 turns across private Hermes, private Pi, and an explicitly authorized project scope.
It includes 16 annotated positive facts, 8 negative turns, and 32 retrieval queries.
Concept groups accept paraphrases without another model acting as a judge. Reports record exact corpus and model digests.
This small synthetic corpus measures regressions. It does not estimate production accuracy or certify a user interface.

The gate requires these minimum scores:

| Metric | Meaning | Minimum |
| --- | --- | --- |
| Precision | Correct extracted facts divided by extracted facts | 0.95 |
| Positive-case recall | Positive cases with a correct extracted fact divided by positive cases | 0.90 |
| Recall@5 | Expected facts found within the first five search results | 0.90 |
| MRR@5 | Mean reciprocal rank of the first correct result within five results | 0.80 |

The gate also requires zero scope leaks, zero provider failures, safe replay, and persistence across Owner restart.
Missing observations fail the gate. After restart, the evaluator repeats all 32 queries.
Record IDs and every returned payload field must remain identical.
The comparison excludes computed similarity scores. The evaluator scores ranking again and retains raw comparison diagnostics.
Event receipts must remain unchanged, and replay must not call the model again.

Latency summaries report measured p50 and nearest-rank p95 for retrieval and model calls.
These percentiles describe the median and the 95th percentile.

## Run the evaluation

Use Python 3.11.
Install `.[remote]`, the Hermes test dependencies, and the MCP checkout's locked Pi dependencies.
Run this command from the Runtime checkout:

```sh
PYTHONPATH="$PWD:/path/to/hermes-agent" python scripts/evaluate_owner_lifecycle.py \
  --adapter-root /path/to/qdrant-memory-mcp --output /tmp/owner-quality-new.json
```

Use a new output path.
Each run creates a private temporary home, synthetic credentials, and temporary Embedded Qdrant data.
The run closes these resources and removes the temporary data.
Reports contain metrics and hashes, without tokens or memory text.

Failures return a nonzero exit status and remain recorded as FAIL.
Observation and recording errors differ from model-provider failures. Both fail the gate.
Restart and receipt replay must not increase model calls.
Model discovery and setup failures also produce a redacted FAIL artifact.

## Committed evidence

| Evidence | Meaning |
| --- | --- |
| [owner-model-quality-evidence.json](owner-model-quality-evidence.json) | Baseline before evaluator review fixes. Raw restart differences affected computed scores only. |
| [owner-model-quality-reviewed-evidence.json](owner-model-quality-reviewed-evidence.json) | Earlier run after evaluator fixes. Includes explicit post-restart model-call and recording-error measurements. |
| [owner-model-quality-drained-evidence.json](owner-model-quality-drained-evidence.json) | Baseline-only PASS after the public Host admission barrier completed before the Provider idle barrier. |
| [owner-mutation-initial-failure.json](owner-mutation-initial-failure.json) | First full baseline and mutation run. FAIL before replacement-intent, Host-admission barrier, and denial-probe fixes. |
| [owner-mutation-quality-evidence.json](owner-mutation-quality-evidence.json) | Historical raw PASS. The cross-Agent handoff claim is withdrawn because both credentials belonged to `pi`. |
| [owner-mutation-identity-evidence.json](owner-mutation-identity-evidence.json) | Fresh full baseline and mutation PASS with stored writer `pi` and independently authorized reader `hermes`. |
| [owner-model-context-evidence.json](owner-model-context-evidence.json) | First full run that observes actual Pi model input. |
| [owner-model-context-reviewed-evidence.json](owner-model-context-reviewed-evidence.json) | Full rerun after context evaluator review. Measures 12 actual Pi model inputs and 38 injected memories. |
| [owner-reviewed-policy-initial-failure.json](owner-reviewed-policy-initial-failure.json) | First full run after fact-pair isolation. Supersession failed. Baseline and context checks passed. Not passing acceptance evidence. |
| [owner-model-stack-evidence.json](owner-model-stack-evidence.json) | Fresh full real-model PASS with source-file fingerprints for the evaluated stack. |

The drained baseline ran all 24 captures, 32 queries, replay checks, and restart checks with real local models.
A Host admission barrier confirms callback delivery. A Provider idle barrier confirms that admitted work has finished.

The initial mutation failure predates the `baseline_executed` and `evaluation_mode` fields.
It does not provide passing evidence for the current candidate.
The historical mutation PASS remains available for traceability, without current acceptance status.
Its `superseded` and `withdrawn_conclusions` fields mark the withdrawn claim. The original measurements remain unchanged.

The policy-review failure predates the opaque CAS token check. The earlier evaluator compared hash strings by order.
CAS tokens have no order. Supersession requires the same record ID and a different token, with no obsolete result.

The revised scorer checks stored provenance and the actual reader grant for distinct logical-Agent identities.
Missing or equal identities fail the gate.
The identity run passed all eleven checks, with 0 obsolete hits from 8 observed hits.
Concurrent compare-and-swap (CAS) produced one winning update and one conflict. Restart, replay, and revocation checks also passed.
CAS accepts an update only when the supplied version matches the stored version.

The first context-observation run measured 12 actual Pi model inputs and 38 injected memories.
It recorded zero annotated errors and zero scope violations. All baseline and mutation gates also passed.

The stack run covered 24 baseline turns and 32 queries.
Precision, positive-case recall, Recall@5, and MRR@5 each scored 1.0. All eleven mutation checks passed.
It measured 12 actual Pi model inputs and 38 injected memories, with zero incorrect injections and zero scope violations.
Restart added zero model calls.
Source-file fingerprints bind this evidence to evaluated files. They do not certify a Git commit, production accuracy, or remote CI execution.

## Driver contract

`pi-quality-driver.yml` defines a contract job on a standard GitHub-hosted runner.
The job uses the fixed Pi SDK and actual extension with a synthetic Owner transport.
It repeats prompts and delays delivery across cases.
Each receipt must keep its original event-to-case association.

The job rejects skipped or absent tests.
This workflow never selects the model runner.
Its result needs a completed CI run.

## Small-model smoke workflow

`owner-model-smoke.yml` runs on relevant pull requests and main changes. It also accepts a manual request.
The current smoke candidate selects `qwen3.5:4b`, with thinking disabled, and `all-minilm:22m` with 384 embedding dimensions.
Both model manifests are fixed in the profile. CI requires five independent lifecycle runs before acceptance.
The smaller `qwen3:0.6b` package is about 523 MB. It failed both initial smoke attempts with empty extraction.
The [explicit-preference attempt](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39586687759) did not complete the native capture check.
The 0.8B model uses the same preference cases and validity checks as the 0.6B attempt.
Package sizes do not measure process memory. The 0.8B package also contains vision components.
The [first 0.8B run](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39610058850) passed all eight smoke checks.
It made two generation requests, with observed p50 6.00 seconds and p95 6.31 seconds.
Ollama reported 574,074,386 loaded bytes for generation and 49,387,928 for embeddings, with zero GPU memory.
These are loaded-model observations from one smoke run, not peak process memory or a quality benchmark.
The [later 0.8B run](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39624059913) failed because Pi project capture returned no memory.
Both generation requests returned HTTP 200. The two earlier passes do not establish repeat stability.
The earlier 0.8B pair required about 1.37 GB of model downloads.
The [first 1.7B run](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39627174644) also failed with empty native capture.
These failures used imperative requests that began with "Remember".
The extractor excludes commands and accepts durable facts. Command interpretation is a possible cause, but remains unverified.
The separate `owner-smoke-declarative-v2` cases state a user preference and a project fact directly.
Reports include this version and its SHA-256 digest. The full quality corpus and validity checks remain unchanged.
The workflow uses the shared setup and caches. It requires no API key or self-hosted runner.
The separate `model-smoke` environment contains no required secrets.

Manual runs can select `smoke-qwen06`, `smoke-qwen08`, `smoke-qwen17`, `smoke-qwen2`, or `smoke-qwen4` through `model_profile`.
These profiles use `qwen3:0.6b`, `qwen3.5:0.8b`, `qwen3:1.7b`, `qwen3.5:2b`, and `qwen3.5:4b`, respectively, with the same embedding model and checks.
Each run selects one model. Pull requests use the single `smoke-ollama` default.
For that default, the optional `OLLAMA_SMOKE_PROFILE` environment variable takes precedence over a secret of the same name.
The value must be one of these five profiles or `smoke-ollama`. Invalid values fail before checkout or model setup.
An explicit manual comparison takes precedence over this override. Five successful repetitions are still required.

The smoke test uses real model calls, Hermes native capture, the Pi SDK, and HTTPS MCP.
Its two synthetic turns state a user preference and a project fact.
Named-subject extraction, multilingual meaning, and supersession remain in the full quality corpus.
It checks authorized recall, private isolation, replay, and durable restart without new generation calls.
It queries the model's captured text to check the storage round trip. It does not score semantic accuracy.
Empty captures, provider failures, invalid payloads, missing observations, or failed checks produce FAIL.
Each repetition uses new Owner data, device credentials, and host state. The Ollama process and fixed weights remain warm.
Any failed repetition stops the run. A later success cannot replace that failure.
The report retains each completed repetition in `runs`; `requested_repetitions` records the required count.
Success is `SMOKE_PASS`, with `quality_evaluated=false` and `baseline_executed=false`.
This result cannot certify the full quality baseline or mutation gates.

The declarative-v2 comparisons failed with [0.6B](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39678861759), [0.8B](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39678867271), and [1.7B](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39678857488).
Each stopped during its first lifecycle with empty capture. HTTP 200 responses did not establish capture success.
The [2B run](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39680318405) extracted a native memory but failed Runtime validation of its importance value.
The Owner blocked the write. The 4B candidate retains the same v2 cases, prompts, embedding model, and gates. Its stability remains unverified.

## Manual quality workflow

The quality mode in `owner-model-quality.yml` runs only by manual request.
This mode does not run on pull requests or forks and uses no production credentials.
It checks out immutable Hermes and MCP candidates.
Select one of these profiles:

| Profile | Runner | Generation | Embeddings |
| --- | --- | --- | --- |
| `openrouter-free` (default quality) | GitHub `ubuntu-24.04` | OpenRouter `liquid/lfm-2.5-2.6b:free` | `qwen3-embedding:0.6b`, 1024 dimensions |
| `hosted-ollama` (optional comparison) | GitHub `ubuntu-24.04` | `qwen3:1.7b` | The same local 0.6B model |
| `hosted-qwen35` (experimental) | GitHub `ubuntu-24.04` | `qwen3.5:2b` | The same local 0.6B model |
| `hosted-qwen35-4b` (experimental) | GitHub `ubuntu-24.04` | `qwen3.5:4b` | The same local 0.6B model |
| `reference` | Disposable Linux runner with `qdrant-memory-models` label | Installed `qwen3:8b` | Installed `qwen3-embedding:4b`, 2560 dimensions |

The first four profiles need no self-hosted runner.
They download a fixed Ollama release and fixed model manifests from [Ollama](https://github.com/ollama/ollama/releases).
The setup checks the archive checksum and model digests in [model-profiles.json](../../tests/quality/model-profiles.json).
It starts a private process on loopback port 19134. It stops that process after the test.
CI caches dependency downloads, the fixed Ollama release archive, and each profile's local models.
Cache keys include the platform and the relevant lock or model configuration hash.
Every restored archive is checked against its fixed SHA-256 before extraction or execution.
Model digests and the running Ollama version are also checked after cache restore.
Credentials, evaluation reports, process IDs, and temporary memory data are excluded from caches.
The model workflow saves verified downloads before quality checks, so a later quality failure does not discard them.
The reference profile retains port 11434 and requires a separately provisioned test runner.
Run the reference profile from `main` only.
The pinned cache actions use version 6.1.0 with Node.js 24. The reference runner must be version 2.327.1 or later.
The setup loads local models before measurement. Reported model latency excludes initial model download and load.

The OpenRouter profile requires the `model-quality` GitHub Environment with the test secret `OPENROUTER_API_KEY`.
No other credential is required. Run this profile from `main` only.
Only its generation step receives the secret. Embeddings remain on the runner.
Only this profile uses the credential environment. All local profiles use `model-smoke`.
The [Free Router](https://openrouter.ai/docs/guides/routing/routers/free-router) selects an available free model for each request.
Reports record the observed model IDs and HTTP status counts.
Model selection can change between requests, so results describe that run only.
The evaluator spaces requests by at least 3.1 seconds. Quotas and service availability can still cause failures.
There is no paid fallback or automatic retry after a provider denial.
A missing secret, rate limit, provider error, or missing model observation cannot produce PASS.
Use synthetic test data only. OpenRouter receives the synthetic generation prompts.

For a fixed quality model, set the manual `generation_model` input to an explicit model ID ending in `:free`.
You can also set `OPENROUTER_QUALITY_MODEL` as an Environment variable or Secret. Prefer a variable for this public model name.
The manual input takes precedence, then the variable, then the Secret. An empty selection uses the fixed Liquid free model.
Select `openrouter/free` explicitly for dynamic exploration.
Paid model IDs and malformed values fail before provider requests. There is no model fallback.
The Owner requests `provider.require_parameters=true` so OpenRouter must select an endpoint that supports the requested structured output parameters.
The existing Owner LLM adapter accepts the optional boolean `provider_require_parameters`; other profiles omit it.
Unsupported endpoints must fail instead of silently discarding required parameters.
See [OpenRouter structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs).

Reports distinguish `fixed-route` from `dynamic-free-router`.
A fixed model requires matching actual response model IDs. Missing, different, or mixed IDs fail the gate.
The `:free` suffix is significant. A response without it does not establish the configured variant.
Free Router results are exploratory. Even a passing score does not provide the fixed-model baseline required for release-candidate acceptance.
Keep the model ID, observed IDs, embedding manifests, dataset and prompt fingerprints with each run.

All profiles use the full frozen corpus and the same quality thresholds.
Reports identify the profile, generation route, embedding dimensions, and local model digests.
Results from a smaller model do not replace the historical reference measurements.
The separate two-subject native Ollama probe runs only for `reference`.
The full baseline and mutation gates run for every profile.
Workflow configuration alone does not prove execution. Record the run URL and actual result separately.

Each dispatch runs one selected profile. It does not run a model matrix.
Use the workflow input to select another fixed model profile. A model name is configuration and needs no Secret.
Model changes require a fixed manifest digest. Arbitrary unverified model tags are not accepted.
The hosted Ollama profiles use the same embedding model, generation budget, and disabled thinking for comparison.
The local 1.7B comparison completes the baseline with low resource use. It has not passed the quality gates.
Keep failed quality results visible. The separate smoke workflow cannot supply a quality PASS.

The [initial hosted run](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39570844284) used `qwen3:4b-instruct-2507-q4_K_M` and failed.
Precision was 1.0, but positive-case recall and Recall@5 were 0.4375. Mutation checks also failed.
Provider errors and scope leaks were zero. This is failure evidence, not acceptance evidence.
The [8B hosted attempt](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39572169059) failed before baseline completion.
Four generation calls reached the existing 30-second request timeout. This result does not prove a memory shortage.
The [3B hosted attempt](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39573848777) also failed before baseline completion.
Five model responses were observed, with no provider or recording errors. A background ValueError blocked the lifecycle.
This is not passing model-quality evidence. The experimental local profile now tests the smaller `qwen3:1.7b` under the same timeouts and quality gates.
The optional Free Router profile uses remote generation and the local 0.6B embedding model.
Its main-branch exploration failed before baseline completion. See the [hosted CI evidence](#hosted-ci-evidence-on-2026-10-21).
The quality thresholds remain unchanged.
Setup records Ollama's loaded-model size in `quality-model-resources.json`. This is not peak process memory.
The 3B attempt reported 2,168,360,795 bytes for generation and 1,237,875,425 bytes for embeddings, with zero GPU memory.
Dependency and historical Ollama installation caches were restored in that run. The changed model set was downloaded and saved under a new key.
Current runs cache the verified archive and extract it again, so cached executables cannot bypass archive validation.
The 1.7B model uses [Apache License 2.0](https://ollama.com/library/qwen3:1.7b); CI downloads it for evaluation and does not bundle its weights.

The [first 1.7B attempt](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39578556397) used provider-default thinking and failed before baseline completion.
It observed 27 successful responses and five 30-second read timeouts. Model-call p50 was 10.74 seconds and p95 was 30.03 seconds.
Ollama reported 1,886,629,395 loaded bytes for generation and 1,237,875,425 bytes for embeddings, with zero GPU memory.
The experimental 1.7B profile now explicitly requests `reasoning_effort: "none"` through the existing Owner LLM adapter.
Ollama documents this as the [OpenAI-compatible control to disable thinking](https://docs.ollama.com/api/openai-compatibility).
Reports record this setting. The 30-second timeout, strict output validation, and quality thresholds remain unchanged.
The Owner LLM adapter accepts optional `none`, `minimal`, `low`, `medium`, `high`, or `xhigh` values when the selected provider supports them.
When omitted, the request keeps the provider default. Reference and OpenRouter profiles omit this option.

The [1.7B run with thinking disabled](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39580559444) completed the baseline but failed the quality gates.
All 29 model requests succeeded. Model-call p50 was 1.14 seconds and p95 was 4.37 seconds.
Precision was 1.0, but positive-case recall and Recall@5 were 0.375. Mutation checks also failed.
Faster valid responses alone do not qualify this model as the default quality model.
The next hosted comparison uses `qwen3.5:2b`. Test `qwen3.5:4b` only if the 2B candidate does not pass.

The [2B comparison](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39581374057) failed before baseline completion.
The model loaded and returned one successful response. A background ValueError then blocked the lifecycle.
The exact validation failure is unverified. This run supplies no complete quality scores and does not prove a memory shortage.
Ollama reported 2,369,666,414 loaded bytes for generation and 1,237,875,425 bytes for embeddings, with zero GPU memory.
This failure permits the next 4B comparison. It does not relax any quality gate.

The [first 4B comparison](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39582200266) also failed before baseline completion.
It observed one successful response before a background ValueError blocked the lifecycle.
Ollama reported 3,146,734,631 loaded bytes for generation, with zero GPU memory.
Neither Qwen3.5 run provides complete quality scores. The exact failure needs a diagnostic rerun.
Diagnostic reports retain background exception types and source stages, without exception messages or local values.

The [4B diagnostic rerun](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39583106933) located the failure in the existing memory importance validation.
The generated importance did not satisfy the finite numeric range from 0 to 1. The Runtime correctly rejected it.
Do not rescale invalid output or relax validation to obtain PASS.
Keep `qwen3:1.7b` with thinking disabled as an optional comparison model.
It completed the baseline with less loaded-model memory and lower latency than the tested Qwen3.5 models.
It still fails the frozen quality thresholds. Neither Qwen3.5 candidate qualifies as a replacement.

### Container alternative

An official [Ollama container](https://docs.ollama.com/docker) can isolate installation dependencies.
It does not add RAM or CPU resources, improve extraction quality, or remove request timeouts.
For a container lane, fix the image digest, start the server before pulling models, and poll the API with a deadline.
Bind the port to loopback, cache a mounted model directory across runs, and remove the test container in an always-run step.
A Docker volume alone does not persist across fresh hosted runners.
Do not use an unbounded `docker logs --follow` command as a readiness check.
The current fixed-binary lane already checks readiness, version, model digests, cleanup, and cache restore.
All three hosted attempts completed setup. Their observed failures do not justify a second installation path.

Ordinary test CI also runs the scorer unit tests.

Both lanes require Linux x86_64 and Python 3.11.
They install the hashed wheels in `requirements-quality.txt`.
The local Runtime build uses the pinned build backend and an offline dependency check.
The installer keeps normal dependency resolution and runs `pip check`.
The hosted driver job verifies this environment without loading models.

To update the snapshot, run this command from the Runtime checkout:

```sh
uv pip compile pyproject.toml requirements-quality.in --extra remote \
  --constraint requirements.txt --python-version 3.11 \
  --python-platform x86_64-unknown-linux-gnu --generate-hashes \
  --no-header --no-annotate --output-file requirements-quality.txt
```

Review the resulting versions and hashes before you use the new snapshot.

## Actual model-input observation

The Pi quality driver observes actual model input at `streamSimple`, after lifecycle context hooks run.
The `context_quality` result measures incorrect annotated memory injection separately from extraction precision and retrieval.
Injection means adding recalled memory to model input.

Missing or duplicate case observations fail the gate.
No measured injections, unknown facts, or incorrect space or visibility also fail the gate.
The read credential grants project access only, including turns that capture into Pi's private write scope.
Injected facts must match the project annotations. This measurement does not assess unrestricted private recall or production factual accuracy.
Malformed memory JSON aborts the driver and produces a redacted FAIL artifact. It cannot count as zero errors.

## Mutation and multilingual extension

Add `--scenarios tests/quality/lifecycle-mutations-v1.json` to run the frozen mutation corpus after the baseline capture, retrieval, and restart gate.
Chinese Hermes turns in separate sessions measure explicit supersession and continuation by the same logical Agent on a second device.
Supersession replaces an existing fact with a newer fact.
Actual Pi hooks capture a Chinese documentation preference and an authorized project architecture decision.

Explicit deletion, replay, and concurrent CAS use verified HTTPS MCP.
Two independently authenticated logical Agents attempt to update one project-memory version concurrently.
The gate requires exactly one update and one conflict.
A project read grant must reject deletion and must fail authentication after revocation.
Another restart checks durable deletion, winning receipts, revocation, retained facts, and zero additional model calls.
Generic network failure cannot count as authorization denial.

A missing or false scenario fails the extension.
An obsolete superseded fact in the measured latest-fact search also fails the extension.
The `obsolete_hit_rate` metric describes only that annotated query. It does not measure production hallucination or overall injection rate.
The baseline and mutation corpora have separate versions and hashes. Baseline evidence alone does not prove extension execution.

The manual model workflow enables this extension explicitly. Record evidence from its actual execution separately.

Use `--scenarios-only` for a focused mutation run.
Its report states `evaluation_mode=mutations-only`, zero baseline cases and queries, and `baseline_executed=false`.
A successful diagnostic reports top-level `status=DIAGNOSTIC_PASS` and `mutation_scenarios.status=PASS`.
Exit status 0 indicates diagnostic success only. It does not certify the v1 baseline.
Failures and exceptions report `status=FAIL` and return exit status 1.

A successful full run reports top-level `status=PASS` and `baseline_executed=true`.
The manual workflow requires both fields and mutation `PASS` for acceptance.
It does not accept `DIAGNOSTIC_PASS` as a full quality result.

The reference profile also tests a turn that replaces facts about two different projects.
It runs the exact-ID update contract with both a deterministic facade and the installed local model.
The model probe uses Ollama's native test API with thinking disabled. It does not change production model routing.
The workflow rejects skipped model probes and stores their JUnit results.

## Explicit supersession

The `replacement_intent` extraction flag requests relation review of up to five existing candidates in the same authorized scope.
The review can include candidates below the ordinary similarity cutoff. The original user statement supplies untrusted evidence.
Only the factual `SUPERSEDES` relation can authorize an update instead of an add.
The new fact must describe the same subject and attribute and give the replacement value.

The full user statement supplies intent evidence. It cannot make a fact about another subject replace the candidate.
The first relation check excludes the full statement. Only a `CONFLICT` result can request a second check with intent evidence.
The flag cannot change the grant, target scope, or write permissions.

New extraction schemas require every property under the [strict Structured Outputs contract](https://developers.openai.com/api/docs/guides/structured-outputs).
Legacy durable candidates with missing or false flags keep the previous review policy.
Embeddings and the stored payload schema remain unchanged. An explicit replacement can require up to ten relation-model calls.
Report the measured latency, including these calls.

## Hosted CI evidence on 2026-10-21

These results describe the recorded runs. They do not establish production acceptance.

| Run | Result | Observed limit |
|---|---|---|
| [Qwen3.5 0.8B smoke](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39613998288) | SMOKE_PASS | All eight functional checks passed. This run did not evaluate full model quality. |
| [Later Qwen3.5 0.8B smoke](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39624059913) | FAIL | Both requests returned HTTP 200, but Pi project capture returned no memory. |
| [Fixed Nemotron free model](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39617795232) | FAIL | The first request returned HTTP 404. The full baseline did not run. |
| [Fixed Dots free model](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39618414645) | FAIL | Ten requests returned HTTP 200 with the exact selected free model ID. The next request returned HTTP 400. The full baseline did not complete. |
| [Free Router exploration](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39619046518) | FAIL | Three requests returned HTTP 200 across two models. The next request returned HTTP 404. The full baseline did not complete. |
| [Fixed Liquid free model](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39624008695) | PASS | The full baseline, mutation checks, and context checks passed. All 29 responses matched the selected free model ID. |

The Dots run selected `dots-studio/dots-3-note-preview:free`.
The exact cause of its HTTP 400 response remains unverified.
Later reports use `failed_model_calls` to identify failed extraction cases by their frozen case IDs.
Relation failures use `relation`; unknown calls use `other` or `extract/unattributed`.
These counters do not contain raw input, response bodies, or exception messages.
The Free Router run observed `liquid/lfm-2.5-2.6b:free` and `dots-studio/dots-3-note-preview:free`.
The fixed Liquid run used `liquid/lfm-2.5-2.6b:free` on Runtime `0b1b05289eae3731af30e1c38c427aec7c6ac635`.
It completed 24 baseline turns, 32 queries, and 11 mutation checks.
Precision was 1.0. Positive-case recall, Recall@5, and MRR were 0.9375.
Scope leaks and provider failures were zero. Restart made no new generation calls.
This records a passing run, not guaranteed endpoint availability or a remote weight checksum.
Each completed external run stopped its test-owned Ollama process.
The runs retained strict output validation, TLS checks, the frozen dataset, and the quality thresholds.
They used free routes without a paid fallback.

The historical [local reference evidence](owner-model-stack-evidence.json) records a full PASS with Qwen3 8B and Qwen3 Embedding 4B.
It applies to its recorded source and model hashes. It does not certify the current hosted CI source or free routes.
Model catalog support does not guarantee that an endpoint is available for every request.
Keep functional smoke acceptance separate from full quality acceptance.
