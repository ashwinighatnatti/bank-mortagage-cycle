# Changelog

All notable changes to this repo are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.0] - 2026-08-24

### Added
- Initial commit: reference design HTML, MVP, and the POC (agentic backend +
  frontend) built against Claude Opus 4.7 on Microsoft Foundry, plus Azure
  deployment notes and the `ai-native-mortgage` design skill.
- Root `README.md` with setup instructions for all three layers.
- `poc/.env.example`, referenced by `poc/README.md` and `poc/.gitignore` but
  missing from the initial commit.
- `LICENSE` (MIT).
- Root `.gitignore` for secrets, Python/Node artifacts, OS and editor files.
- `.gitattributes` normalizing line endings (LF for text, binary for
  images/db files) to fix CRLF/LF checkout warnings on Windows.
- `CONTRIBUTING.md` with setup, secrets handling, and design-rule guidance.
- `.github/CODEOWNERS`.
