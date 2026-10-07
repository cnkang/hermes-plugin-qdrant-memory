# Troubleshooting

Run maintenance commands in the same active profile as the agent. Stop the agent
first when using embedded Qdrant. Commands report sanitized error types rather
than remote response bodies, so inspect service health and configuration directly
without pasting credentials into logs or issues.

| Symptom | Checks and recovery |
| --- | --- |
| Provider not discovered | Confirm the directory is named qdrant-memory under the active profile's plugins directory and contains the root entry point. Confirm a compatible Hermes host and dependencies prepared by Hermes PM. Restart the runtime. |
| Embedding connection or timeout error | Check the configured base URL and service process. For default Ollama, confirm qwen3-embedding:4b is provisioned. Fix service availability before retrying failed work. |
| Dimension, distance or fingerprint mismatch | Restore the previous pipeline configuration, or create a separate compatible target collection and migrate explicitly. Do not change the live collection schema or delete its identity point. |
| Nonempty collection has no trusted fingerprint | It is not a plugin-managed target. Use a new collection; import legacy memories through migration rather than reusing an unknown schema. |
| Embedded store already accessed | Another client owns the local persistence lock. Stop the agent and other maintenance commands. Never remove lock files while their process is alive. |
| `WriterBusyError` / `writer_busy` during `init`, `migrate` or `retry` | A local session or gateway owns the destination's writer lease in any deployment mode. Run `hermes gateway status` and `hermes gateway list`, stop the relevant service with `hermes gateway stop`, exit CLI sessions with `/exit`, or stop a foreground gateway with Ctrl-C. Wait for workers to exit and rerun the command. See the [migration stop/resume/restart procedure](migration-from-mem0.md#json-export-plan-stop-writers-migrate-and-restart). |
| Dry-run succeeds but real migration is refused | Dry-run only plans the source; it does not acquire writer ownership or probe target services. Stop target writers before real migration, including for Server/Cloud. Do not clear the collection or delete lock/ledger files. |
| Cloud authentication/setup failure | Check HTTPS, endpoint and Database API key permissions in the active secret scope. A Cloud management key is not a Database API key. Authentication errors are not retried as transient transport failures. |
| Recall is temporarily empty | The cache may not be populated yet or has been invalidated by a session/author change. Use the explicit search tool to check scoped stored memories. A cache miss performs no synchronous service call. |
| FAILED ledger rows | Correct the service/configuration error, stop the agent if embedded, run retry, and restart the provider for raw-event extraction. Use stats to confirm state transitions. |
| Missing migration operation | Preserve the ledger and source export. Re-run without resume to create a fresh source plan, then verify; never mark the damaged manifest complete by editing counts. |
| Migration verification fails despite matching count | Check the manifest's missing/mismatched IDs. Counts do not establish identity or payload correctness. Preserve the source and rerun the appropriate snapshot. |
| Oversize memory rejected | Inspect text, metadata and total UTF-8 byte limits. Truncate is explicit and applies only to text; metadata and total payload limits still fail. |

## Recovery procedure

```bash
hermes qdrant-memory status
# Stop interactive/foreground writers; for an installed background gateway:
hermes gateway status
hermes gateway stop
hermes gateway status
hermes qdrant-memory doctor
hermes qdrant-memory stats
hermes qdrant-memory retry
hermes qdrant-memory verify
```

Only after recovery and verification succeed, restart the background service if it
was previously running:

```bash
hermes gateway start
hermes gateway status
```

`retry` commits prepared operations but cannot perform trusted LLM extraction from
raw events in maintenance mode. Start the configured provider again to process those
events. `verify` validates existing target records and the latest migration manifest;
it does not rewrite damaged records or create a missing collection.

For migration-specific failure recovery, use the original source and flags:

```bash
hermes qdrant-memory migrate mem0 --source-json /path/to/export.json \
  --target-collection hermes_qdrant_memory --resume --retry-failed --verify
hermes qdrant-memory verify --collection hermes_qdrant_memory
```

Keep writers stopped while running migration recovery. The [migration guide](migration-from-mem0.md)
includes the complete shutdown, verification and restart sequence, including foreground
gateways. Local locks do not stop writers on other machines.

## CI failures

Add SONAR_TOKEN and SNYK_TOKEN as repository Actions secrets, not config or comments.
The Snyk scan environment must include pip for its Python resolver; CI adds that
tool through Hermes PM separately from the plugin's runtime requirements.
Fork scans remain skipped and the required aggregate gate fails deliberately.
CodeRabbit's skipped manual-review status does not certify a code review.
See [CI services](ci.md) for onboarding and quality-gate behavior.
