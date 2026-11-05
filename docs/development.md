# Development and local checks

The additive host-independent CLI and MCP interface is documented in
[standalone setup](standalone.md), including its separate profile, operator-secret
resolution, fixed scope and no-Hermes acceptance requirements.

Use the Hermes host and its dependency manager described in the README. From
the host checkout, build the same isolated Ruff environment used by CI:

```bash
cd /path/to/hermes-agent
python -m pm.build_env --out /path/to/hermes-plugin-qdrant-memory/.lint-env \
  --requirement 'ruff==0.15.1'
cd /path/to/hermes-plugin-qdrant-memory
.lint-env/bin/ruff check .
.lint-env/bin/ruff format --check .
git config --local core.hooksPath .githooks
```

The repository pre-commit hook runs both checks on a temporary copy of the Git
index. It checks exactly what will be committed, including partially staged
files. Unstaged corrections cannot hide a staged lint error. A missing Ruff
environment blocks the commit with setup instructions. The hook does not change
source files or install dependencies. Hook installation is local to this clone.
Git does not activate repository hooks automatically. This local setting takes
precedence over a global hooks directory. Teams with existing hooks should chain
the Ruff checks into their existing pre-commit hook instead.

To apply fixes deliberately, run `.lint-env/bin/ruff check --fix .` and
`.lint-env/bin/ruff format .`. Review the changes, then stage them again. Ruff
checks Python errors, import ordering, Python 3.11 modernization and Google-style
English docstrings. CI also checks formatting. All modules, classes, functions
and methods have English docstrings. Comments explain durability, authorization
and concurrency decisions where the reason is not obvious from the code.

Run behavior tests with the real host's `scripts/run_tests.sh`, using
`HERMES_PYTHON` to select the plugin test environment. `-j 2 --file-retries 0`
runs independent test files in parallel. For coverage, use `-j 1` with
`--cov=qdrant_memory --cov-report= --cov-append`, then export the combined
coverage database. Concurrent processes must not write one coverage database.
See the README for dependency setup and [validation](validation.md) for evidence.

Use serial file execution for the disposable Server REST/gRPC tests: they restart
their shared test container. Supply `--qdrant-test-url` and `--qdrant-test-container`
after the runner's explicit `--`. The host runner clears arbitrary environment
variables. The required CI host matrix validates the minimum supported release and
the latest Hermes `main` (each run records the moving ref's checked-out SHA).
Linux/macOS/Windows compatibility has a separate six-job matrix. See
[CI services](ci.md).

## Hermes verify manifest

`.hermes/environment.json` defines a scoped smoke check for `hermes verify`.
Without the manifest, detection treats this plugin as a FastAPI application. It
would run `uv sync` and a bare `pytest`. That cannot import the host and would
rewrite `uv.lock`.

The manifest runs two repository-contract suites: the CI gate policy and the
dependency-input policy. Both need no Hermes checkout and no Qdrant server. The
bootstrap phase stops with a clear message when `.test-env` is missing.

```bash
hermes verify --json --phase bootstrap --phase test
```

This check is a smoke check. It does not replace the full behavior suite above.
A green result does not certify host behavior, Server persistence or release
readiness.

The current retrieval fixture contains 24 labeled queries. `scripts/evaluate.py`
requires a live embedding service and uses only a temporary collection. For a
separate synthetic ledger recovery/storage measurement with no Qdrant network calls:

```bash
PYTHONPATH=/path/to/hermes-agent .test-env/bin/python scripts/benchmark_recovery.py \
  --sizes 0 1 100 1000 10000 100000
```

This script also uses temporary data. See the [recorded recovery benchmark](recovery-benchmark.md)
for the measured revision, allocation boundaries and retained-history limits.
