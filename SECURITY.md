# Security Policy

## Reporting a vulnerability

This is a POC/demo repository, not a production system, but please report
security issues responsibly rather than opening a public issue:

- Preferred: use GitHub's [private vulnerability reporting](../../security/advisories/new)
  for this repository.
- Otherwise: contact the maintainer listed in
  [`.github/CODEOWNERS`](.github/CODEOWNERS).

Please include steps to reproduce, the affected file/component, and the
potential impact. You should get an initial response within a few days.

## Scope

- `poc/` — the agentic backend and frontend. This is where most
  security-relevant surface area lives: tool gating (`app/gate.py`),
  disposition policy (`app/policy.py`), the audit chain (`app/audit.py`), and
  document handling (`app/documents.py`, including prompt-injection
  containment — see "Adversarial testing" in
  [`poc/README.md`](poc/README.md)).
- `azure-web-app/` — deployment configuration notes. Flag anything here that
  documents an insecure default (e.g. ACR admin credentials over managed
  identity) that you think should change before any non-demo use.
- Static reference files (`Coforge AI-Native Mortgage Demo.html`,
  `mvp/index.html`) contain no backend or secrets and are out of scope.

## Known, accepted trade-offs (not vulnerabilities)

These are documented, deliberate POC/demo simplifications — no need to report
them unless you've found a way they're actually exploitable beyond what's
described:

- Azure hosting uses ACR admin username/password rather than managed
  identity, and SQLite data is ephemeral (resets on restart) — see
  [`azure-web-app/AZURE_DEPLOYMENT_NOTES.md`](azure-web-app/AZURE_DEPLOYMENT_NOTES.md).
- The synthetic loan book in `poc/backend/data/` deliberately includes a
  hostile document (`LN-2026-0003`) used to test prompt-injection
  containment — this is test fixture data, not a live exploit.

## Secrets

Never commit `poc/.env` or any real API key — see the "Secrets" section in
the root [`README.md`](README.md). If you find a secret committed anywhere in
this repo's history, report it privately as above rather than opening a
public issue, so it can be rotated before disclosure.
