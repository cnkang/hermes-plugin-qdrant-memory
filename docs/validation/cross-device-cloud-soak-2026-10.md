# 24-hour cloud soak (2026-10-27/28)

Two cloud ARM64 hosts ran a full 24-hour window with no local machine in the
loop. The run used the quarantine fix on main. The conclusion separates
product behavior from three harness defects found during the run.

## 1. Baseline

| Item | Value |
| --- | --- |
| Run id | `cloud-2026-10-27` |
| Window | 1440 min, 2026-10-27T07:18Z to 2026-10-28T07:18Z |
| Runtime | main `f843556` (contains the event quarantine fix) |
| MCP | main `b33b09a` |
| Skill | `1fed111`, version 0.3.4 |
| Server A | 2x Neoverse-N1, 4 GB RAM, Ubuntu 26.04 ARM64, private `10.0.3.184` |
| Server B | 2x Neoverse-N1, 4 GB RAM, Ubuntu 26.04 ARM64, private `10.0.14.107` |
| Embedding | `qwen3-embedding:4b`, 2560 dims, Cosine, server-side Ollama loopback |
| Extraction | `deepseek-v4.1-flash` through `https://ollama.com/v1`, `json_schema`, 2048 tokens |
| Agents | OpenCode 1.19.4 on both hosts, Pi 0.73.1 on B |
| Transport | Owner loopback-only, nginx TLS proxy on the private address, port 9443 |
| Owner restarts | 330 controlled restarts, median down 5 s, median ready 2 s |

SSH served only for install and evidence collection. Systemd units ran the
Owner, both load workers and the restart timer. A four-minute no-SSH gap
confirmed the run continues without the control session.

## 2. Pre-run verification

A 20-minute precheck passed on both hosts. A two-host barrier with identical
text returned `ADD` on one side and `SKIP` with the same record id on the
other side. Pi automatic capture committed with `created_by=pi-b` and
`source=owner_lifecycle`. An isolated-profile drill passed 7 of 7 quarantine
cases. These runs used synthetic markers only.

## 3. Window results

Server A journaled 501 rows. Server B journaled about 1632 rows. The ledger
holds 2115 `COMMITTED` operations and zero `FAILED` rows. Events hold 18
`COMMITTED` and 2 `QUARANTINED` rows. Private checks leaked zero records in
25 attempts.

| Path | Measured rate | Verified reading |
| --- | --- | --- |
| Direct write plus read | about 94.5 percent | Restart-boundary failures excluded, the path held |
| Agent write plus bare-marker check | 6.5 percent on A, 2.3 percent on B | Measurement artifact, see section 4 |
| Pi capture | 33 percent | Same artifact plus one real quarantine, see section 5 |
| Restart-boundary direct writes | 75 of about 1650 committed | 23 uncommitted, see section 6 |

The `report.py` verdict is `FAIL` on both hosts because failures exist. The
rate above needs the three corrections below before it can describe the
product.

## 4. Bare-marker checks undercount agent writes

Agent rows verify with the bare marker only, for example
`SOAKA-AG-cloud-2026-10-27-250`. The store holds hundreds of near-duplicate
facts with the same 24-character prefix. The bare marker falls out of the
default top 8 while the full stored sentence returns rank 1 with score 1.0.
Every sampled agent failure, including rows with transport errors, resolved
to rank 1 with the exact text. The agent success counter therefore measures
retrieval ranking, not write loss. The direct path uses the full sentence and
kept 94.5 percent. The next run should use random marker suffixes.

## 5. Slow extraction quarantines one Pi turn

One Pi event recorded `QUARANTINED` with `OwnerExtractionTruncated` and
`retryable=false`. The turn took over 99 seconds. The memory never landed.
Slow turns between 54 and 150 seconds failed while turns under 25 seconds
succeeded. This is a write-side loss inside the designed quarantine path. It
did not block unrelated writers. One more quarantine appeared near the end
of the window with the same reason.

## 6. Restart-boundary writes can stay uncommitted

Direct failures at the restart boundary split into two groups. Most failures
kept the committed record while only the response was lost. A minority of 23
rows never landed. The client maps a restart cut to `remote_error` with
`retryable:false` because only timeout and unreachable count as retryable. A
caller that honors the contract drops these writes permanently. The Owner
restarted cleanly all 330 times with no double lease and no WAL damage.

## 7. Harness defects found during the run

1. `report.py` shipped without the executable bit. The B worker crashed at
   the window end when it tried to run the report. Systemd restarted the
   worker into a new window and the resume path deleted the finished B
   journal. About 284 B rows after 03:08Z survive only as hourly watchdog
   copies and aggregates. B totals below are reconstructed, not measured.
2. The resume path unlinks the journal instead of archiving it under the
   finished run id.
3. The B coordinator lacks `try/except` around the agent and Pi branches. Two
   coordinator crashes came from this gap. Systemd healed both with no window
   reset.
4. `search_marker` uses the bare marker. Section 4 covers the effect.

## 8. Verdict

**SOAK COMPLETED.** The full window ran on both hosts with intact journals
on A and a reconstructed tail on B. Product findings: quarantine contained
two slow extractions, private isolation held with zero leaks, restarts stayed
short and clean, and 23 restart-boundary writes exposed a retryable
contract gap. The low agent success counter is a harness metric defect, not a
storage loss: every sampled agent row exists at rank 1 under its exact text.
The next run needs random markers, an archived journal on resume, an
executable report step, and a decision on retryable classification for
restart cuts.
