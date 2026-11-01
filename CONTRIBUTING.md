# Contributing

Thanks for improving Qdrant Memory for Hermes. This document covers the fast
path to a mergeable change. The deeper references live in [docs/](docs/).

## Before you start

- Bugs and feature requests go through the issue templates. Security issues:
  do **not** open an issue. Follow [SECURITY.md](SECURITY.md).
- For anything larger than a small fix, open an issue first so we can agree on
  the approach before you invest time.

## Development setup

Requirements: Python 3.11+, a compatible Hermes checkout, and Ollama able to
serve `qwen3-embedding:4b` for the live embedding paths.

1. Clone this repository and `NousResearch/hermes-agent` side by side.
2. Build the isolated lint environment and enable the staged pre-commit hook as
   described in [docs/development.md](docs/development.md).
3. Run behavior tests through Hermes's canonical runner rather than `pytest`
   directly. [docs/development.md](docs/development.md) documents the exact
   invocation, including the disposable Qdrant Server container flags.

For standalone changes, install the reviewed checkout with `python -m pip install
'.[mcp]' pytest` in an isolated environment and run
`python -m pytest -q tests/standalone`. This acceptance suite needs no Hermes
checkout or live model. It uses real embedded storage and a localhost fixture.
The standalone CI matrix requires Linux/macOS/Windows on Python 3.11 and 3.14.

## Checks that must pass

Every pull request must pass the same gates CI enforces:

```bash
.lint-env/bin/ruff check .
.lint-env/bin/ruff format --check .
# canonical runner: from the Hermes checkout, see docs/development.md
```

- Ruff lint and formatting.
- The canonical Hermes test runner on Python 3.11 and 3.14 (CI runs the minimum
  supported release and the latest Hermes `main`. Locally one host is enough).
- Pull requests require SonarCloud, Snyk Code and the required security-scan
  gate. The credential-free dependency-lock policy also runs on each PR; the
  Snyk dependency scan runs only on trusted `main` so its organization token
  never reaches contributor-controlled code. See [docs/ci.md](docs/ci.md).

The repository pre-commit hook checks exactly what will be committed. Install
it so a staged snapshot cannot hide a lint error.

## Pull requests

- Keep the diff focused. Describe what changes and why using the PR template.
- Commit messages: `type: concise subject` (`fix:`, `feat:`, `docs:`, `test:`,
  `chore:`).
- Update `CHANGELOG.md` for user-visible behavior changes.
- Update the docs in the same PR when behavior, commands or configuration change.
- Add a regression test for bug fixes. Keep tests deterministic and isolated
  (no live network beyond the documented disposable services).

## Reporting security issues

See [SECURITY.md](SECURITY.md). Do not disclose vulnerabilities in public
issues or pull requests.
