# Changelog

All notable user-facing changes to Game Localizer will be documented in this file.

The project follows semantic-version-style release numbering. Until the first tagged release, the current codebase is considered pre-1.0 and interfaces may still evolve.

## [Unreleased]

### Added

- Repository contribution and security policies.
- Structured issue and pull-request templates for public maintenance.
- OSS project-status and provenance information in the root README files.
- Package metadata links for documentation, source, and issue tracking.

## [0.1.0] - Planned

The first tagged release is intended to establish the current localization pipeline as a reproducible baseline, including:

- resource scanning and normalized translation units;
- SQLite translation memory with source fingerprints and stable coordinates;
- OpenAI-compatible model-assisted translation workflows;
- deterministic QA for placeholders, terminology, residual source-language text, filtering, and normalization;
- auditable human revision, recovery, and incremental rebuilds;
- preview and QualityGate-protected release artifacts;
- local, GitHub Releases, Cloudflare R2, and Alibaba Cloud OSS publishing targets;
- local dashboard workflows;
- bilingual documentation and cross-platform automated tests.

The exact tagged release notes should be generated from the final release commit so this changelog does not claim an artifact has been published before it exists.