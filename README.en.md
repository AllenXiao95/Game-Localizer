<p align="center">
  <img src="assets/game-localizer-logo.png" alt="Game Localizer" width="720">
</p>

# Game Localizer

[简体中文](README.md) | **English** | [日本語](README.ja.md)

Game Localizer is a localization pipeline for game text. It brings resource scanning, translation memory, machine translation, quality assurance, human revision, artifact building, and publishing together in one traceable workflow.

The project is driven by declarative configuration and supports individual games, multiple resource directories, and continuous updates across versions. By default, SQLite is used as the authoritative data source for translation memory (TM).

## Key features

- Supports Gettext PO/MO, ParaTranz JSON, and Paradox YAML resources.
- Connects to translation models through an OpenAI-compatible API, with concurrency, rate limiting, and local tokenizer support.
- Manages the SQLite TM using stable coordinates and source-text fingerprints, preventing outdated translations from being reused after the source changes.
- Distinguishes machine translations, human-approved translations, and historical migration records to protect reviewed content.
- Provides checks for placeholders, residual source-language text, terminology, filtering, and normalization.
- Supports `preview` and `release` build modes; release artifacts must pass the QualityGate.
- Preserves run state, checkpoints, reports, and manifests, allowing failed runs to resume or builds to be incrementally recreated from a parent run.
- Provides a local observability dashboard for inspecting runs, locating QA issues, and submitting auditable human revisions.
- Supports publishing to a local directory, GitHub Releases, Cloudflare R2, and Alibaba Cloud OSS.

## Requirements

- Python 3.10 or later
- Git (for version control; the pipeline itself does not require a remote repository)

Install the development version:

```powershell
python -m pip install -e .
```

Install optional features as needed:

```powershell
# AES-256 encrypted artifacts
python -m pip install -e ".[artifact-aes]"

# Hugging Face tokenizer
python -m pip install -e ".[tokenizer-huggingface]"

# All remote publishing adapters
python -m pip install -e ".[publish-all]"
```

## Quick start

### 1. Start the Dashboard (recommended)

The Dashboard is the preferred entry point for everyday work: start local tasks, inspect runs, locate QA issues, submit human revisions, and trigger incremental rebuilds. After installation, start it immediately with the bundled example:

```powershell
localizer dashboard projects/example/project.yaml --host 127.0.0.1 --port 8765
```

Then open <http://127.0.0.1:8765>. The Dashboard can open without an API key, but machine translation tasks require the project configuration and credentials described below. Write operations are enabled only on a loopback address.

### 2. Prepare the project directory

Keep each game's configuration and rules in a separate directory:

```text
projects/my-game/
├── project.yaml
├── prompt.md
├── background.md
├── glossary.yaml
└── rules.yaml
```

Minimal `project.yaml` example:

```yaml
schema_version: 1

project:
  id: my-game
  name: My Game
  game_version: 1.0.0

paths:
  source: ../../data/my-game/source
  workspace: ../../var/my-game/workspace
  output: ../../var/my-game/output

languages:
  source: en-US
  target: zh-Hans

resources:
  adapters:
    - type: gettext
      include:
        - "**/*.po"
        - "**/*.mo"
      options:
        layout: standard
        empty_source: skip
        source_filter: all

prompt:
  template: ./prompt.md
  background: ./background.md

glossary:
  file: ./glossary.yaml
  auto_discovery: candidate_only

rules:
  file: ./rules.yaml

provider:
  type: openai_compatible
  base_url: https://api.example.com/v1
  api_key_env: LOCALIZER_API_KEY
  model: provider-model-name
  concurrency: 4
  timeout_seconds: 120
  context_window: 32768
  max_output_tokens: 4096

tm:
  database: ../../var/my-game/localizer.sqlite
  global_exact_match: reviewed_only
  commit_policy: quality_gate

workflow:
  mode: local

build:
  format: zip
  release_channel: stable
  artifact_prefix: localization
  compression: deflate
  encryption: none

publish:
  targets:
    - type: local
      destination: ../../releases/my-game
      versioned_prefix: true
```

Relative paths in the configuration are resolved from the directory containing `project.yaml`. Credential fields accept environment variable names only; never write secrets into YAML.

Prepare the supporting files:

```yaml
# glossary.yaml
schema_version: 1
terms: []
```

```yaml
# rules.yaml
schema_version: 1
```

`prompt.md` should specify the target language, writing style, and formatting constraints, and instruct the model to preserve placeholders in the input. The optional `background.md` supplies game-world and UI context. The pipeline assembles the exact batch format expected in model responses.

See [`projects/example`](projects/example/project.yaml) for a complete annotated configuration with glossary, rules, and resource cases.

### 3. Set credentials

PowerShell:

```powershell
$env:LOCALIZER_API_KEY = "your-api-key"
```

You can also use a `.env` file and enable automatic discovery in the configuration's `environment` section. The `.env` file must remain outside version control.

### 4. Validate and scan

```powershell
localizer validate-config projects/my-game/project.yaml
localizer scan projects/my-game/project.yaml
```

### 5. Build a preview with the CLI (optional)

```powershell
localizer build projects/my-game/project.yaml --mode preview --run-id preview-001
```

A preview run generates output, QA reports, and run records, but does not present them as publishable release artifacts.

The Dashboard can start preview tasks directly. The CLI command below is intended for scripts, CI, and unattended runs.

### 6. Build and publish a release artifact with the CLI

```powershell
localizer build projects/my-game/project.yaml --mode release --run-id release-001
localizer publish projects/my-game/project.yaml path/to/artifact-manifest.json
```

`release` runs the full quality gate. Only a manifest that passes the gate can enter the publishing workflow.

## Common workflows

### Everyday Dashboard workflow

```powershell
localizer dashboard projects/my-game/project.yaml --host 127.0.0.1 --port 8765
```

Human revisions made in the Dashboard record the operator and an append-only decision log, and are synchronized to the production TM. Configuration files remain under version control; the Dashboard handles execution, observability, and controlled revision.

### Incrementally rebuild from a parent run

This is useful when a release check has failed and the issues have been corrected manually, but you do not want to request every machine translation again:

```powershell
localizer rebuild-from-run projects/my-game/project.yaml `
  --parent-run-id release-001 `
  --run-id release-002 `
  --mode release
```

The command validates source-text fingerprints, reuses still-valid results from the parent run, and reprocesses only unresolved entries.

### Multiple resource variants

A project can declare multiple resource directories under `paths.sources` and select one using `paths.default_variant` or the CLI's `--variant` option. Each variant has its own run and output directories, while sharing the same TM, glossary, and rule set.

```powershell
localizer build projects/my-game/project.yaml --variant beta --mode preview --run-id beta-001
```

### Copy an existing release artifact only

```powershell
localizer publish-local path/to/artifact-manifest.json path/to/destination
```

## Translation memory (TM)

SQLite is the authoritative source for runtime translations and human revisions. Production writes follow these principles:

- A coordinate is jointly determined by the project, adapter, relative path, and logical key.
- Lookups also verify the source-text fingerprint; when the source changes, the old translation is not returned as a direct match.
- Human-reviewed records have higher priority than machine results and cannot be silently overwritten by bulk operations.
- Machine translations are committed as production records only after passing the quality gate.
- Every change retains its origin, state, run identifier, and audit information.

When migrating from a legacy system, synchronize and verify first, then switch the authoritative source:

```powershell
localizer tm-sync-legacy projects/my-game/project.yaml path/to/legacy-tm.json

# Generate a difference report only (default)
localizer tm-adopt-artifact projects/my-game/project.yaml path/to/artifact-manifest.json

# Write to SQLite after human confirmation
localizer tm-adopt-artifact projects/my-game/project.yaml path/to/artifact-manifest.json `
  --apply `
  --accepted-by project-owner

localizer tm-verify-artifact projects/my-game/project.yaml path/to/artifact-manifest.json `
  --run-id verify-001
```

`tm-switch-authority` is a governance action, not a routine build step. Before running it, preserve the behavioral baseline, data baseline, and legacy TM, and confirm that rollback and audit materials are complete.

## Directories and artifacts

Typical run data is stored in the configured `workspace` and `output` directories:

```text
var/my-game/
├── localizer.sqlite
├── workspace/
│   └── <run-id>/
│       ├── checkpoints/
│       ├── reports/
│       └── run-state.json
└── output/
    ├── preview/
    └── release/
```

Run directories contain reproducible data. Release artifacts and their manifests are the entry points for publishing and read-back verification. Do not infer the version, mode, or quality status from an archive filename alone.

## Publishing and security

- Local publishing does not require a credential-governance declaration.
- Remote publishing is disabled by default and is enabled only after credentials have been rotated and audit records configured.
- Configuration files may reference environment variable names only; they must not contain tokens, passwords, or access keys.
- Each publishing target runs independently. A failed target does not damage the local artifact and returns a retryable result.
- Production releases should pin the version, preserve the manifest, and perform read-back verification on uploaded results.

## Testing

Run the complete test suite:

```powershell
python -X utf8 -m unittest discover -s tests
```

Before committing, it is also recommended to run:

```powershell
python -m pre_commit run --all-files
```

## Example project

- [Minimal runnable configuration](projects/example/project.yaml)
- [Example game background](projects/example/background.md)
- [Example translation prompt](projects/example/prompt.md)
- [Example glossary](projects/example/glossary.yaml)
- [Example rules](projects/example/rules.yaml)
- [Example Gettext resource](projects/example/source/messages.po)

## Current limitations

- The configuration model and offline synchronization components for community-platform workflows exist, but an online API client has not yet been implemented.
- The web dashboard is intended for local observability and targeted single-user revision; it is not an authenticated collaboration platform with task assignment and multi-user approval.
- Remote publishing requires the relevant optional dependencies, environment credentials, and explicit governance configuration.

## License

This project is distributed as source-available software under the [PolyForm Noncommercial License 1.0.0](LICENSE):

- You may use, study, modify, and distribute it in the noncommercial scenarios defined by the license.
- Commercial use is outside the public license and requires prior, separate authorization from the licensor.
- Because commercial use is restricted, this project is not open-source software under the OSI definition.
- This license applies to versions distributed with this `LICENSE`; existing grants for historical versions obtained under other licenses are not retroactively revoked.
- Third-party dependencies and external resources remain subject to their respective licenses.

See [LICENSE](LICENSE) for the complete, legally binding terms.
