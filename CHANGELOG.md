# Changelog

All notable user-facing changes to Game Localizer will be documented in this file.

The project follows semantic-version-style release numbering. Pre-1.0 interfaces may still evolve as the framework is generalized across more games and localization workflows.

## [Unreleased]

No entries yet.

## [0.1.0] - 2026-08-24

First public release baseline of Game Localizer.

### Added

- Resource scanning and normalized translation-unit abstractions for Gettext PO/MO, ParaTranz JSON, and Paradox YAML workflows.
- SQLite translation memory with stable coordinates, source fingerprints, migration/bootstrap support, and stale-entry recovery.
- OpenAI-compatible model-assisted translation with configurable concurrency, rate limiting, and tokenizer support.
- Deterministic QA for placeholders, terminology, residual source-language text, filtering, normalization, and release readiness.
- Auditable human revision, failure recovery, checkpoints, and incremental rebuilds.
- `preview` artifacts and QualityGate-protected `release` artifacts.
- Publishing targets for local directories, GitHub Releases, Cloudflare R2, and Alibaba Cloud OSS.
- Local dashboard workflows for observability and targeted single-user revision.
- Bilingual Chinese/English documentation site and task-oriented setup/configuration guides.
- Cross-platform automated test matrix covering Windows and Ubuntu with Pydantic 1.x and 2.x.
- Full-history secret scanning in CI.
- Repository contribution and security policies, structured issue forms, and pull-request review guidance.
- Public M8 roadmap for controlled agent workflows, Prompt evaluation, governed build orchestration, and a future Tauri desktop client.

### Current limitations

- Community-platform workflows have configuration and offline synchronization components but no online API client yet.
- The dashboard is designed for local observability and single-user revision rather than multi-user approval workflows.
- Remote publishing requires the relevant optional dependencies, environment credentials, and explicit governance configuration.
- M8 agentic workflows and the Tauri client remain a design/roadmap milestone and are not part of this release.
