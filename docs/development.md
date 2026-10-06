# Development and local checks

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
files; unstaged corrections cannot hide a staged lint error. A missing Ruff
environment blocks the commit with setup instructions. The hook does not change
source files or install dependencies. Hook installation is local to this clone;
Git does not activate repository hooks automatically. This local setting takes
precedence over a global hooks directory; teams with existing hooks should chain
the Ruff checks into their existing pre-commit hook instead.

To apply fixes deliberately, run `.lint-env/bin/ruff check --fix .` and
`.lint-env/bin/ruff format .`, review the changes, then stage them again. Ruff
checks Python errors, import ordering, Python 3.11 modernization and Google-style
English docstrings. CI also checks formatting. All modules, classes, functions
and methods have English docstrings; comments explain durability, authorization
and concurrency decisions where the reason is not obvious from the code.

Run behavior tests with the real host's `scripts/run_tests.sh`, using
`HERMES_PYTHON` to select the plugin test environment. `-j 2 --file-retries 0`
runs independent test files in parallel. For coverage, use `-j 1` with
`--cov=qdrant_memory --cov-report= --cov-append`, then export the combined
coverage database. Concurrent processes must not write one coverage database.
See the README for dependency setup and [validation](validation.md) for evidence.
