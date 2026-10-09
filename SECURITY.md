# Security Policy

## Supported versions

The latest released version and `main` receive security fixes. This project is
a local-first Hermes plugin, not a hosted service.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's
[Security Advisories](https://github.com/cnkang/hermes-plugin-qdrant-memory/security/advisories):
open the repository's **Security** tab and choose **Report a vulnerability**.
Do not open a public issue for a suspected vulnerability.

Include, as far as you can:

- affected version or commit, and the deployment mode (embedded / server / cloud),
- reproduction steps or a proof of concept,
- the impact you believe it has (data exposure, injection, denial of service),
- any suggested fix.

You can expect an initial response within a few days. Please allow time for a
fix and a release before public disclosure. We credit you in the release
notes unless you prefer otherwise.

## Scope

In scope: the plugin package (`qdrant_memory/`), its CLI, and the documented
Hermes integration in this repository. Examples: credential handling and
scope leaks, prompt-injection paths through stored memory, unsafe defaults, or
path and permission issues.

Out of scope: vulnerabilities in Hermes Agent itself (report them to
`NousResearch/hermes-agent`), in Qdrant (report them through the
[Qdrant bug bounty program](https://qdrant.tech/security/bug-bounty-program/)),
in Ollama (report them through the
[Ollama security policy](https://github.com/ollama/ollama/security/policy)),
or in services you configure. Reports that require pre-existing local shell access or
a compromised host are usually out of scope. This project does not run a bug
bounty program.

## Handling expectations

- Keep credentials in Hermes's secret scope. Logs and issues must never contain
  API keys, tokens, or memory payloads.
- [docs/security.md](docs/security.md) describes the deployment security
  boundaries of this plugin. It is operational guidance, not a disclosure
  policy.
