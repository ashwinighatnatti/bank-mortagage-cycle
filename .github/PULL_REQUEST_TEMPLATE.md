## What & why

<!-- What does this change do, and why is it needed? Link any related issue. -->

## Which layer(s) does this touch?

- [ ] Reference design (`Coforge AI-Native Mortgage Demo.html`) / `mvp/`
- [ ] `poc/backend`
- [ ] `poc/frontend`
- [ ] `azure-web-app` deployment notes
- [ ] `.claude/skills/ai-native-mortgage`
- [ ] Repo meta (README, CI, etc.)

## Checklist

- [ ] No secrets committed (`.env`, API keys, tokens) — see [SECURITY.md](../SECURITY.md)
- [ ] If `poc/backend/app/` agent, tool, or policy code changed: separation of
      duties is intact (an agent that raises a finding doesn't also hold a
      repair tool; disposition lane still comes from `policy.decide_disposition()`,
      not the model) — see [CONTRIBUTING.md](../CONTRIBUTING.md)
- [ ] If a `requirements*.txt` changed: both lock files were regenerated
- [ ] Tests pass locally (`pytest` in `poc/backend`, `npm test` in `poc/frontend`)
- [ ] `poc/scripts/verify_synthetic_data.py` passes, if synthetic data
      generation changed
- [ ] `CHANGELOG.md` updated under `[Unreleased]`

## How was this tested?

<!-- Commands run, screenshots, or manual steps taken to verify the change. -->
