# Cross-device shared memory acceptance: OpenCode and Pi (2026-10)

Two physical devices, three agent instances, one Owner. This page records what ran,
what passed, and which defects the run exposed. The sharing, update, concurrency,
isolation and lifecycle results come from real OpenCode or Pi processes on the
named device. The transport and recovery probes use the documented CLI, HTTPS and
socket interfaces. Section 6 reproduces each defect with direct commands. Checks
that could not run are labelled rather than counted.

## Result summary

| Group | Scope | Cases | PASS | FAIL |
| --- | --- | --- | --- | --- |
| A | Basic sharing, three clients | 9 | 9 | 0 |
| B | Update, delete, CAS, idempotency | 12 | 12 | 0 |
| C | Controlled concurrency and dedupe | 5 | 5 | 0 |
| D | Authorization, isolation, transport | 9 | 9 | 0 |
| E | Pi automatic capture and recall | 6 | 6 | 0 |
| F | Restart, crash, outage replay, backup | 7 | 6 | 1 |
| **Total** | | **48** | **47** | **1** |

The single failure (F6b) is a retained-event extraction failure. The pending event
replayed exactly once, and the Owner kept it durable. The configured model
produced output that exceeded its token budget. The write did not commit until an
operator raised the budget and retried.

## 1. Scope and topology

```
Device A (Owner host)                       Device B (client host, point-to-point link)
  OpenCode 2.0.24  --stdio bridge-->          OpenCode 2.1.3 --stdio bridge-->
                                     \                          \
                                      > HTTPS 443 (private CA)   > HTTPS MCP (same endpoint)
  Owner: Runtime 0.5.0                         Pi 1.4.0 + owner-memory.mjs
    remote-serve (loopback 8765/8766)
    TLS terminator (loopback -> peer address)
    Embedded Qdrant + SQLite Ledger
    qwen3-embedding:4b (2560 dimensions)
```

One Owner owns every write, the embedding pipeline and the Ledger. No client holds
a Qdrant credential or a database path. Clients hold one provisioned bearer
credential each.

## 2. Versions and commits

| Component | Version | Revision / package |
| --- | --- | --- |
| Runtime | 0.5.0 | tag `v0.5.0` = `60762c3e39f4cb3419f6e983aeb61833952bf216` (PyPI 0.5.0 wheel) |
| MCP distribution | 0.2.4 | `ba3afa30a818a76ae9348cb7605fcbf7c14c3010` (PyPI 0.2.4) |
| Skill | 0.3.3 | `5bac228e8594a6cf8ff642c22f0f0e100ebca3a6` |
| Owner client library | `mcp==2.3.0`, `httpx==0.28.1`, `qdrant-client==1.20.0` | installed with the Runtime wheel |
| Device A | macOS 27.0.1 arm64 | Python 3.13.7, OpenCode 2.0.24 |
| Device B | Omarchy 4.0.6 / Arch x86_64, kernel 7.2.4 | Python 3.14.8, Node 26.12.0, OpenCode 2.1.3, Pi 1.4.0 |
| Owner extraction model | `deepseek-v4.1-flash:cloud` through a loopback Ollama endpoint | `response_format_mode: json_schema`, `max_output_tokens: 2048` |
| Owner embedding | `qwen3-embedding:4b`, 2560 dimensions, Cosine | fingerprint `8b5dffff…14a6b91` |

## 3. Isolated environment

- Dedicated Owner profile, collection, Ledger, policy and credentials. No existing
  Hermes profile, memory collection or service was opened or reconfigured.
- Eight independent credentials: `opencode-a`, `opencode-b`, `pi-b`, `reader`
  (read-only), two private-space credentials, one unrelated private space, and one
  revocable credential used for the revocation check. Only SHA-256 hashes live in
  the policy.
- Private CA with an IP and DNS subject alternative name. Clients trust that CA
  explicitly (`--ca-file` / `QDRANT_MEMORY_CA_FILE`). No system trust store was
  modified, and TLS verification was never disabled.
- `remote-serve` keeps the Owner on literal loopback. A same-host TLS terminator
  binds one interface address. The Owner rejects every request whose `Host` is not
  the configured authority.
- OpenCode ran with private XDG roots and `--standalone`, so the reviewer's
  installed configuration and background service were untouched. Pi ran with
  `PI_CODING_AGENT_DIR` and a separate session directory.
- Both agents used one model relay bound to the point-to-point interface. The
  relay carries no credentials and no stored memory. It exists because the Owner's
  embedding and extraction models stay loopback-only.

## 4. Test groups

### A. Basic sharing

| Case | Client | Result |
| --- | --- | --- |
| A1 | OpenCode A creates a shared record | PASS, `created_by=opencode-a`, id and revision returned |
| A2 | OpenCode B reads it in a new session | PASS, same id and creator |
| A3 | Pi reads it in an independent session | PASS, same id |
| A4 | OpenCode B creates a second record | PASS, `created_by=opencode-b` |
| A5 | OpenCode A reads B's record | PASS |
| A5b | Pi reads B's record | PASS |
| A6 | Pi creates a record | PASS, `created_by=pi-b` |
| A6b | OpenCode A reads Pi's record | PASS |
| A6c | OpenCode B reads Pi's record | PASS |

Provenance, scope, visibility and revision were re-read through a separate
read-only credential, not taken from the tool response.

### B. Update, delete, CAS and idempotency

| Case | Result |
| --- | --- |
| B1 OpenCode A creates | PASS |
| B2 OpenCode B reads id and revision | PASS |
| B3 Pi updates with the current revision | PASS, `updated_by=pi-b`, revision changed |
| B4 OpenCode A reads the update | PASS |
| B5 OpenCode B updates with the stale revision | PASS, `SharedConflictError` |
| B6 The stale attempt changed nothing | PASS, text and revision unchanged |
| B7 OpenCode B updates with the current revision | PASS, `updated_by=opencode-b` |
| B8 Identical request replay | PASS, same receipt returned |
| B9 Same request id with different arguments | PASS, `SharedConflictError` |
| B10 Authorized delete | PASS, `DELETE` acknowledged, record gone |
| B11 All three clients see the deletion | PASS, no resurrection |
| B12 Delete replay | PASS, idempotent `DELETE` |

No lost update, stale read, duplicate point or wrong attribution appeared.

### C. Concurrency

| Case | Result |
| --- | --- |
| C1 Three agents submit identical text with different request ids | PASS, one record, one `ADD` and dedupe `SKIP` elsewhere |
| C2 Three semantically similar but different facts | PASS, three distinct records |
| C3 Two competing updates on one revision | PASS, one `UPDATE`, one `SharedConflictError` |
| C4 Independent updates on different records in parallel | PASS, both applied |
| C5 Ledger state after the batch | PASS, no `PENDING`/`FAILED` operations |

The operation volume was bounded: three writers and two updates. This is a
correctness check, not a load test.

### D. Authorization and isolation

| Case | Result |
| --- | --- |
| D1a Read-only credential searches | PASS, records returned |
| D1b Read-only credential cannot add, update or delete | PASS, `PermissionError` on all three writes |
| D2 OpenCode A cannot read another device's private space | PASS, invisible to both project and unrelated private grants |
| D3 OpenCode B cannot mutate a known id from a foreign private space | PASS, `PermissionError` |
| D4 Identity fields in tool arguments are rejected | PASS, rejected at the client and again at the Owner |
| D5 Revoked credential fails immediately, restore works | PASS, HTTP 401 while revoked |
| D6 Accepted write whose response was dropped replays once | PASS, one record |
| D7 Clients cannot reach Qdrant directly | PASS, embedded backend, no listening 6333/6334, no key in policy |
| D8 TLS, Host, Origin and bearer rejections | PASS, untrusted CA rejected, wrong `Host` 403, `Origin` 403, bad token 401 |

### E. Pi automatic capture and recall

| Case | Result |
| --- | --- |
| E0 Owner extraction preflight | PASS, event `COMMITTED` |
| E1 A natural-language preference is captured without a memory tool call | PASS, record created by `pi-b` |
| E2 Both OpenCode devices read the captured record | PASS |
| E3 A fresh Pi session recalls the preference with no explicit search | PASS |
| E4 Correcting the preference supersedes the record | PASS, one corrected record remains |
| E5 Recall is not written back as a new fact | PASS, record count unchanged |

Automatic capture required an explicit project capture rule on the Pi credential.
Without that rule the same turn stays private. That check was not bypassed.

### F. Failure and recovery

| Case | Result |
| --- | --- |
| F2 Clean restart | PASS, records persist |
| F3 Crash (`SIGKILL`) and restart | PASS, records persist, no duplicate |
| F3b Writes resume after the crash | PASS |
| F6a Pending event replays once after an Owner outage | PASS, exactly one durable event |
| F6b The replayed event commits | FAIL, output truncated at the configured 800-token budget. It committed only after the budget was raised and `retry` ran. |
| F9 Backup, restore and restart | PASS, 1.6 MB profile archive, record restored |
| F10 Latency samples | PASS, see section 5 |

`NOT APPLICABLE`: direct Qdrant outage. Embedded storage lives inside the Owner
process, so there is no separate service to stop. `NOT VERIFIED`: embedding
service outage. Stopping the shared Ollama service would affect unrelated
applications on device A and was outside the authorized scope.

## 5. Performance observations

One-shot CLI round trip (process start + TLS handshake + MCP initialize + one
search), five samples:

| Path | Samples (ms) |
| --- | --- |
| Device A, same host | 455, 452, 418, 464, 463 |
| Device B, over the link | 900, 896, 896, 917, 888 |

The Owner's own counters on this machine reported embedding p50 ≈ 96 ms and
Qdrant query p50 ≈ 2.6 ms. Most of the observed round trip is client process
startup and the MCP handshake, not the memory operation. Extraction is the slow
path. An early lifecycle phase used `qwen3:8b`, and its `extract_llm_latency_ms`
p50 was ≈ 13 s per turn. The final configuration uses
`deepseek-v4.1-flash:cloud`, which answers in a few seconds. A lifecycle turn
returns only after extraction commits.

## 6. Findings

### 6.1 Runtime: `retry` could not open an enrolled destination (fixed)

- **Repro**: enroll a destination, stop the Owner, run
  `qdrant-memory --home OWNER retry` without a policy.
- **Expected**: the documented recovery command requeues retained work.
- **Actual**: `PermissionError`. The destination claim requires a protected
  policy that the maintenance command never supplied.
- **Root cause**: `cli._run` acquired the writer lease with no destination
  binding, while `WriterLease.for_config` refuses an enrolled claim without one.
- **Fix**: `retry` accepts `--policy`, validates it through the same
  cwd-containment and symlink checks as the Owner, and hands it to the lease. The
  refusal stays when the flag is absent.
- **Regression test**: `tests/security/test_shared_security.py::test_retry_passes_the_protected_policy_to_the_writer_lease`.

### 6.2 Runtime: a relative `--home` failed inside read-only SQLite access (fixed)

- **Repro**: `qdrant-memory --home ./profile verify` (or `list`, `stats`,
  `doctor`, `retry`).
- **Expected**: the same behaviour as the absolute path.
- **Actual**: `ValueError` from `Path.as_uri()` on a relative path.
- **Root cause**: `home` was never normalized before
  `has_pending_reset_file` built a SQLite URI.
- **Fix**: resolve `--home` once at the CLI boundary.
- **Regression test**: `tests/standalone/test_remote_listener.py` sibling case
  `tests/security/test_shared_security.py::test_relative_home_reaches_the_same_state_paths`.

### 6.3 Runtime: the loopback listener could not rebind after a restart (fixed)

- **Repro**: serve the HTTPS Owner, make one request, stop it with `SIGTERM`,
  start it again immediately.
- **Expected**: the restart binds the same port.
- **Actual**: `OSError` (sanitized), with no reason. On this host the address
  stayed unusable for at least 20 s.
- **Root cause**: accepted connections leave the address in `TIME_WAIT`, and the
  listener socket did not set `SO_REUSEADDR`.
- **Fix**: bind through `remote_gateway.listener_socket`, which sets the option.
  It never lets two live listeners share a port.
- **Regression test**: `tests/standalone/test_remote_listener.py`.

### 6.4 Configuration: the extraction budget must fit the model (documented)

- **Repro**: run the lifecycle with a reasoning-capable extraction model and the
  default `max_output_tokens: 800`.
- **Actual**: the relation call ends with `finish_reason=length`. The Runtime
  raises `ValueError("Owner extraction output was truncated. Retain the event.")`
  and keeps the event.
- **Impact**: the turn produces no memory. The Owner blocks new writes until an
  operator intervenes. The budget is therefore a deployment decision, not a detail.
- **Guidance**: set `max_output_tokens` to at least 2048 for reasoning models, or
  use a model that answers the schema directly. The Owner's LLM client timeout is
  30 s. A slow local model therefore needs a small budget, or you must replace
  the model.
- **Status**: documentation only. Raising the default would trade one failure mode
  for a timeout on slow local models.

### 6.5 Behaviour: a failed lifecycle event blocks every writer (unresolved)

- **Repro**: any deterministic extraction or relation failure during a lifecycle
  turn (see 6.4), then restart.
- **Actual**: the event is retained `FAILED`. The Owner refuses to start while a
  failed event exists. Every client write returns `SharedRecoveryRequiredError`.
  `cancel` refuses an event whose extraction was already prepared, and `retry`
  re-runs the same input.
- **Assessment**: the durability contract is correct. Nothing is lost, and nothing
  is silently rewritten. The availability contract is strict. One malformed model
  response removes the whole shared service until the operator fixes the model and
  retries. A per-event quarantine, or an explicit operator discard for prepared but
  unwritten events, would keep other clients serving.
- **Status**: not changed in this round. It alters a documented failure contract
  and needs its own design and review.

### 6.6 Client UX: rejected credentials surface as `ExceptionGroup` (unresolved)

- **Repro**: call `remote-call` with a revoked or wrong credential.
- **Actual**: the request is correctly rejected with HTTP 401, but the CLI prints
  `{"error": {"type": "ExceptionGroup"}}`, which does not distinguish
  authentication from transport failure.
- **Status**: reported only. The denial itself is correct and immediate.

### 6.7 Distribution docs described a retired preview (fixed)

`integrations/shared/README.md`, `integrations/pi/README.md`,
`docs/architecture.md` and `docs/cross-harness-architecture.md` still told readers
to install Runtime 0.4.0 and described the HTTPS endpoint as a source preview.
They now state the shipped pins (Runtime 0.5.0, distribution 0.2.4, launcher
0.1.0-rc.4, Skill 0.3.3) and one broken release-note link was corrected.

### 6.8 OpenCode compatibility note (not a defect)

OpenCode 2.0.24 accepts `mcp.servers.<name>` but rejects an integer `timeout`
with a normalisation warning ("skipped malformed recognized value"). Only the
object form is accepted. A project-level `opencode.json` is read in addition to
the global file. This matters for integration guides, not for the Runtime.

## 7. Fixed issues

| Repository | Commit | Change |
| --- | --- | --- |
| Runtime | `66cd649` | `retry --policy` admission and relative `--home` resolution, with two regression tests |
| Runtime | `63b19af` | both languages document the policy input and its cwd containment |
| Runtime | `7b0c7e9` | `SO_REUSEADDR` on the Owner listener, with a rebind regression test |
| Runtime | `983d9d6` | the socket option stays off Windows, where it permits port sharing |
| Runtime | `01eeb74` | call-site coverage notes and the standalone home boundary test |
| Runtime | `934ba7e` | the retry policy file passes the private-state check before trust |
| Runtime | `80bbd55` | a policy without a destination object is an explicit error |
| MCP | `9dbb2a8` | integration guides and version tables aligned with the shipped pins |

Each fix has a regression test that fails on the pre-fix code. The retry test
captures the lease input. The relative-home test runs the installed entry point
from a relative path. The listener test leaves a server-side `TIME_WAIT` before it
rebinds.

## 8. Unresolved issues and limits

1. A failed lifecycle event blocks every writer until the operator repairs the
   model and retries (6.5).
2. Extraction quality is model-dependent. The same input shape produced a
   truncated or schema-invalid answer with two different models at 800 tokens.
3. Authentication failures through the stdio bridge print an opaque
   `ExceptionGroup` (6.6).
4. The Owner's LLM timeout is fixed at 30 s, so a slow local model cannot use a
   large output budget.
5. Only pre-provisioned credentials are supported. There is no OAuth, no
   interactive consent and no group administration.
6. This run used one Owner on embedded storage. Cloud, self-hosted Qdrant with
   JWT scoping and collection-level key isolation were not exercised here.

## 9. Security and data-isolation conclusions

- One Owner, one Ledger, one embedding pipeline. No client wrote to Qdrant and no
  client held a database credential. The embedded backend exposes no port.
- Every credential resolved to exactly one principal, space and permission.
  Read-only credentials were refused at the service boundary, not only in the
  client schema.
- Private spaces stayed invisible across devices and across logical agents, and a
  known id from a foreign space could not be mutated.
- Identity could not be asserted through tool arguments. The Owner rejected those
  fields on the server side as well.
- Revocation applied to the next request, and a restored credential worked again.
- TLS verification was never disabled: an untrusted CA, a mismatched hostname, a
  wrong `Host`, an `Origin` header and a bad token were all rejected.
- Synthetic data only. No personal memory, credential or production endpoint is
  recorded in this report or in the acceptance artifacts.

## 10. Reviews

Three independent sources reviewed the Runtime change. Reports stayed outside the
repository. This section keeps the disposition only.

| Source | Scope | Blocking findings | Disposition |
| --- | --- | --- | --- |
| Kiro CLI (ACP, read-only worktree) | `60762c3..7b0c7e9`, then the fix delta | P0/P1 none | P2 and P3 items triaged below |
| OpenCode (ACP, read-only worktree) | `60762c3..7b0c7e9`, then the fix delta | P0 none, one P1 | P1 fixed, one P1 shown to be a false positive |
| CodeRabbit (local CLI, committed diff) | all nine changed files | none blocking | one minor doc finding, fixed |

### 10.1 CodeRabbit

One minor finding: the `--policy` example did not state that the policy must
resolve inside the invocation directory. Fixed in both languages, and the
example now uses `./owner-policy.json` after switching to the policy directory.

### 10.2 Kiro CLI

Accepted:

- P2: the retry regression test proved the CLI wiring against a stubbed lease,
  not the real admission path. The test now states the split coverage
  explicitly, and the lease-level test that drives `admit_claim` on an enrolled
  destination is named in the same docstring. A single test that drives the CLI
  through real admission is not reachable with the fixture's isolated registry
  and an uninitialised store. The two tests together cover the path.
- P3: the relative-home test covered `cli._run` only. A named test now locks the
  standalone entry point resolution as well.

Recorded, not changed:

- P3: only `retry` accepts `--policy`. `init`, `migrate` and `delete-all` keep
  their previous admission rules on purpose. The recovery path is the scope of
  this change.

### 10.3 OpenCode

Accepted and fixed:

- P1: the explicit policy file is now checked through `private_entry` before its
  destination binding is trusted, matching the Owner's own admission rule.
- P2: a policy without a destination binding now raises `ValueError` instead of
  behaving like no policy, with a regression test.
- P2: the listener docstring states the POSIX scope of the socket option.

Rejected with evidence:

- P1: the loopback private API "can also fail with `EADDRINUSE`". Its server
  inherits `ThreadingHTTPServer`, and `http.server.HTTPServer.allow_reuse_address`
  is `True`, so the option is already applied there.
  `python3 -c "import http.server; print(http.server.HTTPServer.allow_reuse_address)"`
  prints `True` on the tested interpreter.

Recorded, not changed:

- P3: `cli._destination_policy` imports a helper from `standalone`. Moving it to
  a shared module is a wider refactor than this fix round needs.

### 10.4 Fix round

The same two ACP sources reviewed the fix delta against a six-item claim
checklist. Both marked all six VERIFIED, found no new P0/P1, and confirmed that
the fail-closed refusal for an enrolled destination is preserved. Two P3 polish
items were applied afterwards. The relative-home assertion now compares in the
same normalization the entry point uses. A policy with a missing or non-object
destination binding is reported explicitly. The full suite was re-run on the
final commit.

## 11. Commits and pull requests

| Repository | Content | Pull request |
| --- | --- | --- |
| Runtime | three fixes, their regression tests, the documentation updates and this record | [#89](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/89) |
| MCP | documentation alignment with the shipped pins | [#42](https://github.com/cnkang/qdrant-memory-mcp/pull/42) |

The Runtime pull request keeps the fixes and their evidence together so a
reviewer can check each claim against the commits in one place. The distribution
change contains documentation only.

## 12. Recommendations

1. Raise `max_output_tokens` to at least 2048, or pin a model that answers the
   schema directly, before enabling lifecycle capture in production.
2. Watch `events` for `FAILED` rows. They stop the Owner at the next start.
3. Run the Owner under a supervisor that restarts it, and keep `--policy`
   available to the operator for `retry`.
4. Treat the Pi capture rule as a separate grant: private capture is the default
   and stays private without it.
5. Before scaling to more devices, decide the per-event failure policy (6.5).

## 13. Answers to the acceptance questions

- **Do the three agents share one memory store?** Yes. Records written by
  OpenCode A, OpenCode B and Pi were read by the other two with the original id,
  creator and revision.
- **Can both devices write and read in both directions?** Yes, through the single
  Owner over verified TLS.
- **Does Pi automatic memory work in a shared space?** Yes, with an explicit
  capture rule. The captured records were visible to both OpenCode devices.
- **Is authorization isolation reliable?** Yes for the tested boundaries:
  read-only, private spaces, foreign ids, forged identity fields, revocation.
- **Are outage, restart and failure recovery reliable?** Restart, crash, outage
  replay and backup restore passed. A deterministic extraction failure is durable
  but service-wide (6.5).
- **Any duplicate writes, lost memories or stale facts?** None observed: dedupe,
  CAS, request-id replay and delete were consistent, and supersession replaced the
  corrected preference.
- **Is it ready for ordinary users?** Not yet. The core sharing and isolation
  behaviour is sound. Lifecycle capture still needs model guidance and the failure
  policy in 6.5. A shared deployment is not safe to operate before those are
  settled.
- **What must be fixed first?** The event-failure policy (6.5), an actionable
  error for authentication failures (6.6), and documented model budgets (6.4).

## Appendix A. Reproduction

The layout below is parameterized. `$ROOT` is the owner-side working directory.
`$PEER` is the second device's SSH target. `$HOST` is the Owner's pinned HTTP
authority. `$URL` is `https://$HOST/mcp`.

```sh
# Owner profile, isolated from every existing profile
python3 -m venv "$ROOT/env"
"$ROOT/env/bin/pip" install 'hermes-plugin-qdrant-memory[remote]==0.5.0'
"$ROOT/env/bin/qdrant-memory" setup --profile "$ROOT/owner" \
  --embedding-provider ollama --model qwen3-embedding:4b --dimensions 2560 \
  --base-url http://127.0.0.1:11434 --user-id owner-admin

# Policy: one grant per client, plus the protected destination binding
# (build the binding with the documented public helpers, then add grants).
"$ROOT/env/bin/qdrant-memory" shared-doctor --policy "$ROOT/owner-policy.json"

# Owner: loopback listeners plus the same-host TLS terminator
cd "$ROOT"
"$ROOT/env/bin/qdrant-memory" --home "$ROOT/owner" remote-serve \
  --policy ./owner-policy.json --local-port 8765 --port 8766 \
  --allowed-host "$HOST" --enable-lifecycle

# Client bridge (each client uses its own credential file)
QDRANT_MEMORY_REMOTE_TOKEN="$(cat "$ROOT/tokens/opencode-b.token")" \
  "$ROOT/env/bin/qdrant-memory" remote-mcp --url "$URL" \
  --token-env QDRANT_MEMORY_REMOTE_TOKEN --ca-file "$ROOT/tls/owner-ca.pem"

# Read a record through a separate credential
echo '{"query":"release checklist"}' | \
  QDRANT_MEMORY_REMOTE_TOKEN="$(cat "$ROOT/tokens/reader.token")" \
  "$ROOT/env/bin/qdrant-memory" remote-call --url "$URL" \
  --ca-file "$ROOT/tls/owner-ca.pem" --tool qdrant_memory_search
```

The three defects reproduce directly:

```sh
# 1. Enrolled destination
# Pre-fix behavior (published 0.5.0): the command fails with PermissionError
"$ROOT/env/bin/qdrant-memory" --home "$ROOT/owner" retry
# Post-fix usage: the explicit policy admits the enrolled destination
"$ROOT/env/bin/qdrant-memory" --home "$ROOT/owner" retry --policy ./owner-policy.json

# 2. Relative profile path (pre-fix: ValueError from a path URI)
"$ROOT/env/bin/qdrant-memory" --home ./owner verify

# 3. Immediate restart after one request (pre-fix: address already in use)
READER_TOKEN=$(cat "$ROOT/tokens/reader.token")
curl --cacert "$ROOT/tls/owner-ca.pem" -X POST "https://$HOST/mcp" \
  -H "Authorization: Bearer $READER_TOKEN" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{}'
# stop the Owner, then start it again without waiting
```
