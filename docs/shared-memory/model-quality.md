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

## Manual workflow

The `owner-model-quality.yml` workflow runs only by manual request.
It has no pull-request or fork trigger and uses no production credentials.
It checks out immutable Hermes and MCP candidates.
Select one of these profiles:

| Profile | Runner | Generation | Embeddings |
| --- | --- | --- | --- |
| `openrouter-free` (default) | GitHub `ubuntu-24.04` | OpenRouter `openrouter/free` | `qwen3-embedding:0.6b`, 1024 dimensions |
| `hosted-ollama` (experimental) | GitHub `ubuntu-24.04` | `qwen2.5:3b` | The same local 0.6B model |
| `reference` | Disposable Linux runner with `qdrant-memory-models` label | Installed `qwen3:8b` | Installed `qwen3-embedding:4b`, 2560 dimensions |

The first two profiles need no self-hosted runner.
They download a fixed Ollama release and fixed model manifests from [Ollama](https://github.com/ollama/ollama/releases).
The setup checks the archive checksum and model digests in [model-profiles.json](../../tests/quality/model-profiles.json).
It starts a private process on loopback port 19134. It stops that process after the test.
CI caches dependency downloads, the fixed Ollama installation, and each profile's local models.
Cache keys include the platform and the relevant lock or model configuration hash.
Model digests and the running Ollama version are checked after cache restore.
Credentials, evaluation reports, process IDs, and temporary memory data are excluded from caches.
The model workflow saves verified downloads before quality checks, so a later quality failure does not discard them.
The reference profile retains port 11434 and requires a separately provisioned test runner.
The pinned cache actions use version 6.1.0 with Node.js 24. The reference runner must be version 2.327.1 or later.
The setup loads local models before measurement. Reported model latency excludes initial model download and load.

The OpenRouter profile requires the `model-quality` GitHub Environment with the test secret `OPENROUTER_API_KEY`.
No other credential is required. Run this profile from `main` only.
Only its generation step receives the secret. Embeddings remain on the runner.
The [Free Router](https://openrouter.ai/docs/guides/routing/routers/free-router) selects an available free model for each request.
Reports record the observed model IDs and HTTP status counts.
Model selection can change between requests, so results describe that run only.
The evaluator spaces requests by at least 3.1 seconds. Quotas and service availability can still cause failures.
There is no paid fallback or automatic retry after a provider denial.
A missing secret, rate limit, provider error, or missing model observation cannot produce PASS.
Use synthetic test data only. OpenRouter receives the synthetic generation prompts.

All profiles use the full frozen corpus and the same quality thresholds.
Reports identify the profile, generation route, embedding dimensions, and local model digests.
Results from a smaller model do not replace the historical reference measurements.
The separate two-subject native Ollama probe runs only for `reference`.
The full baseline and mutation gates run for every profile.
Workflow configuration alone does not prove execution. Record the run URL and actual result separately.

The [initial hosted run](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39570844284) used `qwen3:4b-instruct-2507-q4_K_M` and failed.
Precision was 1.0, but positive-case recall and Recall@5 were 0.4375. Mutation checks also failed.
Provider errors and scope leaks were zero. This is failure evidence, not acceptance evidence.
The [8B hosted attempt](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39572169059) failed before baseline completion.
Four generation calls reached the existing 30-second request timeout. This result does not prove a memory shortage.
The [3B hosted attempt](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39573848777) also failed before baseline completion.
Five model responses were observed, with no provider or recording errors. A background ValueError blocked the lifecycle.
This is not passing model-quality evidence. The experimental local profile retains this model for reproducible diagnosis.
The default workflow now uses Free Router generation and the local 0.6B embedding model. Its actual result requires a separate main-branch run.
The quality thresholds remain unchanged.
Setup records Ollama's loaded-model size in `quality-model-resources.json`. This is not peak process memory.
The 3B attempt reported 2,168,360,795 bytes for generation and 1,237,875,425 bytes for embeddings, with zero GPU memory.
Dependency and Ollama installation caches were restored in that run. The changed model set was downloaded and saved under a new key.
The 3B model uses the [Qwen Research License](https://ollama.com/library/qwen2.5:3b); CI downloads it for evaluation and does not bundle its weights.

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

The manual workflow also tests a turn that replaces facts about two different projects.
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
