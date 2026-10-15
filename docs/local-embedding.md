# Optional local CPU embedding and guided setup

These commands are available in this source revision. They are not part of the
published Runtime 0.2.2 or MCP distribution 0.1.3. Until a release includes them,
the published packages continue to use their existing configuration workflow.

## Choose your embedding provider

The existing default remains Ollama. CPU embedding is optional and is never
selected because Ollama or a cloud service happens to be unavailable. Existing
profiles, models, fingerprints and collections are not changed by installation.

| Choice | Requirements | Model download by this Runtime |
| --- | --- | --- |
| Ollama | Running service and installed embedding model | None |
| OpenAI-compatible | Endpoint, model, dimensions and any required key | None |
| FastEmbed CPU | Optional `local` extra and explicitly prepared model cache | About 252 MB for the pinned model/tokenizer files |

The CPU model is multilingual MiniLM-L12-v2, 384 dimensions, using the
[Qdrant ONNX artifacts](https://huggingface.co/Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q)
and [FastEmbed](https://qdrant.github.io/fastembed/). The model is listed as
Apache-2.0. Files are pinned to an immutable model revision and checked against
exact sizes and SHA-256 values. Dependencies and installed environments require
additional disk space. No remote Python model code is downloaded or executed.

## Set up a new private profile without editing JSON

For a reviewed source checkout, install the desired extras into a virtual
environment. The `mcp` extra is needed for MCP, while `local` is needed only for
CPU embedding:

```sh
python -m pip install '.[mcp,local]'
```

Choose **one** setup command and replace the profile path with a new absolute
private directory. The command writes the configuration and initializes embedded
Qdrant using the existing Runtime initialization, writer lease and validation.
It does not install Python or prepare WorkBuddy's runtime environment.

With an existing Ollama model (the `local` extra is unnecessary):

```sh
qdrant-memory setup --profile /absolute/new-profile --embedding-provider ollama --model qwen3-embedding:4b --dimensions 2560 --agent-id workbuddy
```

With an explicitly chosen CPU model:

```sh
qdrant-memory setup --profile /absolute/new-profile --embedding-provider fastembed --agent-id workbuddy
```

With an OpenAI-compatible embedding service:

```sh
qdrant-memory setup --profile /absolute/new-profile --embedding-provider openai-compatible --base-url https://embedding.example.com/v1 --model YOUR_MODEL --dimensions YOUR_DIMENSIONS --agent-id workbuddy
```

Replace the example endpoint/model/dimensions with the service's actual values.
Provide `EMBEDDING_API_KEY` through a private process environment or a secret
manager. Do not pass its value as a command argument. To use a different variable
name, supply `--embedding-api-key-env VARIABLE_NAME`.

Use a different profile and `--agent-id` for each client. `--user-id` defaults to
`local-user`. Self-hosted/Cloud Qdrant storage continues to use the existing
[configuration workflow](configuration.md); this setup command creates only a
new embedded destination. A remote embedding choice is distinct from remote
Qdrant storage.

Once setup succeeds, configure the client's stdio command as `qdrant-memory`
(or the installed `qdrant-memory-mcp` alias), with arguments:

```text
--home /absolute/new-profile mcp
```

The MCP tool catalogue remains search, add, update and delete. Model downloads
and profile creation are trusted CLI operations, not conversational tools.
WorkBuddy needs the launcher executable on its PATH or its absolute path.
This source feature alone does not establish marketplace one-click installation.

## Prepare the CPU model separately

Installers can download the model before attempting MCP connection:

```sh
qdrant-memory prepare-model
```

`--cache-dir /absolute/model-cache` chooses a separate cache. Use the same cache
with `setup --model-cache-dir /absolute/model-cache`, or set the profile's
`embedding.model_cache_dir`. The default model-only cache follows platform
conventions: macOS Library/Caches, Windows LOCALAPPDATA and Linux XDG_CACHE_HOME
or `~/.cache`. It is separate from memory profiles, Qdrant data and the ledger.
Managed cache/profile paths must be canonical absolute paths without symlinks
or Windows reparse points, including intermediate directories.

Preparation requires network access and may take several minutes. Start MCP
after preparation and setup complete; do not place the download inside a
30-second connection handshake. Normal provider startup verifies cached files
and performs no downloads. Prepared CPU inference works offline and does not
send memory/query text to an embedding service.

Downloads use a provisioning lock and a private staging directory. The complete
cache is published only after every file passes validation. Failed preparation
can be retried with the same command. A corrupt completed cache is rejected and
left untouched; it is not silently replaced. Select a new cache directory for
explicit re-preparation, preserving the old files for diagnosis or recovery.

## Safety, limits and recovery

- Repeating the same setup request preserves memories. Setup refuses an unrelated
  directory or a request changing the owned profile's configuration or scope.
- A failed initialization can be retried with the same settings after fixing its
  dependency/service problem. Existing data is never reset by setup.
- CPU input is limited to **128 tokens including instruction prefixes**. Longer
  input is rejected rather than silently truncated. Keep facts concise, or choose
  an explicitly configured provider/model supporting longer inputs.
- Cache location does not change CPU embedding identity. Model revision,
  artifact hashes, preprocessing and the pinned FastEmbed version do.
- Choosing CPU for a collection created with Ollama or another model is not a
  migration. Existing incompatible collections fail compatibility checks.
- `LocalModelNotPreparedError`: run explicit preparation with the correct cache.
- `LocalModelIntegrityError`: do not bypass checks; explicitly prepare a new cache.
- `LocalEmbeddingUnavailableError`: install the reviewed `local` extra.
- `ManagedProfileConflictError`: retain the existing profile and choose a new
  directory, or use the existing advanced configuration/maintenance workflow.

Ordinary clients may still send recalled text to their chat model. Local embedding
does not change the client's own data handling. No automatic conversation
extraction, new network listener or cloud credential custody is introduced.

## Validation boundary

Real local CPU inference and offline official-MCP CRUD/restart tests are separate
from package installation and real WorkBuddy GUI acceptance. The optional local
embedding workflow tests native Linux x64, Windows x64, macOS Apple Silicon and
macOS Intel with Python 3.11. A successful dependency resolution alone is not a
platform support claim. ARM64 variants outside macOS require separate acceptance.
