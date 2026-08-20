<p align="center">
  <img src="assets/game-localizer-logo.png" alt="Game Localizer" width="720">
</p>

# Game Localizer

[简体中文](README.md) | **English**

Game Localizer is a localization pipeline for game text, not a machine-translation tool. It brings resource scanning, translation memory, model-assisted translation, quality assurance, human revision, artifact building, and publishing together in one traceable workflow.

The project is driven by declarative configuration and supports individual games, multiple resource directories, and continuous updates across versions. By default, SQLite is used as the authoritative data source for translation memory (TM).

## Production background

Game Localizer grew out of several years of maintaining Chinese localization for the live-service game *Mir Tankov*; the localization resources and release metadata are maintained publicly in [`tanki-i18n-metadata`](https://github.com/AllenXiao95/tanki-i18n-metadata). The framework generalizes resource processing, quality assurance, and artifact publishing practices validated through real game updates into a reusable pipeline for other games and community localization projects.

## Key features

- Supports Gettext PO/MO, ParaTranz JSON, and Paradox YAML resources.
- Connects to translation models through an OpenAI-compatible API, with concurrency, rate limiting, and local tokenizer support.
- Manages the SQLite TM using stable coordinates and source-text fingerprints, preventing outdated translations from being reused after the source changes.
- Distinguishes model-generated translations, human-approved translations, and historical migration records to protect reviewed content.
- Provides checks for placeholders, residual source-language text, terminology, filtering, and normalization.
- Supports `preview` and `release` build modes; release artifacts must pass the QualityGate.
- Preserves run state, checkpoints, reports, and manifests, allowing failed runs to resume or builds to be incrementally recreated from a parent run.
- Provides a local observability dashboard for inspecting runs, locating QA issues, and submitting auditable human revisions.
- Supports publishing to a local directory, GitHub Releases, Cloudflare R2, and Alibaba Cloud OSS.

## Requirements

- Python 3.10 or later
- Git (for version control; the pipeline itself does not require a remote repository)

For a first local checkout, create an isolated environment and install the complete feature set. Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[all]"
```

Or use Conda:

```powershell
conda env create -f environment.yml
conda activate game_localizer
python -m pip install -e ".[all]"
```

`.[all]` is recommended for first-time users. It includes the Hugging Face tokenizer, AES artifacts, and all remote publishers. For a minimal installation, use `python -m pip install -e .` and add features as needed:

```powershell
# AES-256 encrypted artifacts
python -m pip install -e ".[artifact-aes]"

# Hugging Face tokenizer
python -m pip install -e ".[tokenizer-huggingface]"

# All remote publishing adapters
python -m pip install -e ".[publish-all]"
```

`tokenizer-huggingface` is conditionally required: if `project.yaml` contains `provider.tokenizer`, install it before analysis or translation. Remove the entire tokenizer block to use the built-in conservative token estimator instead. See [First-time setup](docs/en/getting-started.md#choose-an-installation-profile).

## Documentation

- [First-time setup](docs/en/getting-started.md): venv/Conda, installation profiles, credentials, and the first run.
- [General usage guide](docs/en/usage.md): scanning, preview, review, release builds, and publishing.
- [`project.yaml` reference](docs/en/project-configuration.md): semantic explanations for every configuration section.
- [Build TM from existing translations](docs/en/tm-bootstrap.md): ingest supported resources or convert an unknown structure to neutral TM Seed files.
- [TM and SQLite guide](docs/en/translation-memory.md): initialization, legacy JSON migration, verification, authority switching, and rollback.

## Quick start

### 1. Start the Dashboard (recommended)

The Dashboard is the preferred entry point for everyday work: start local tasks, inspect runs, locate QA issues, submit human revisions, and trigger incremental rebuilds. After installation, start it immediately with the bundled example:

```powershell
localizer dashboard projects/example/project.yaml --host 127.0.0.1 --port 8080
```

Then open <http://127.0.0.1:8080>. The Dashboard can open without an API key, but model-assisted translation tasks require the project configuration and credentials described below. Write operations are enabled only on a loopback address.

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

This is useful when a release check has failed and the issues have been corrected manually, but you do not want to request a complete new set of model-generated translations:

```powershell
localizer rebuild-from-run projects/my-game/project.yaml `
  --parent-run-id release-001 `
  --run-id release-002 `
  --mode release
```

The command validates source-text fingerprints, reuses still-valid results from the parent run, and reprocesses only unresolved entries.

### Multiple resource variants

A project can declare multiple resource directories under `paths.sources` and select one using `paths.default_variant` or the CLI's `--variant` option. The Dashboard exposes the same choice as a resource-environment selector. GET APIs use `?variant=<name>`; write APIs, preflight, task snapshots, and presets use a `variant` field. Each variant has its own run and output directories while sharing the same TM, glossary, rules, and serialized task queue.

```powershell
localizer build projects/my-game/project.yaml --variant beta --mode preview --run-id beta-001
```

To vary the public artifact name and publish path with the resource variant:

```yaml
build:
  variant_overrides:
    stable: {variant: desktop, compatibility_env: DESKTOP}
    beta: {variant: beta, compatibility_env: BETA}
```

This mapping controls the artifact name, release slug, versioned upload directory, and compatibility `metadata.json` environment together.

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

New projects do not need hand-written SQL or a pre-created database. The first write workflow, such as `localizer build`, creates the parent directory, SQLite file, schema, and indexes. Creating the schema does not make preview candidates formal; formal writes still follow the build mode and QualityGate. See the [TM and SQLite guide](docs/en/translation-memory.md).

If you only have existing translated resources and no old TM, inspect a supported resource structure directly or import neutral Seed files:

```powershell
# Dry-run existing translations that configured adapters can read
localizer tm-bootstrap-resources projects/my-game/project.yaml

# Convert an unknown structure to one or more neutral TM Seed files
localizer tm-import-seed projects/my-game/project.yaml path/to/ui-seed.json path/to/items-seed.json
```

After reviewing a clean report, add `--apply --accepted-by <operator>`. See [Build TM from existing translations](docs/en/tm-bootstrap.md) for the format and single-/multi-file examples.

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

This project is free software licensed under the [GNU General Public License v3.0 or later](LICENSE) (SPDX: `GPL-3.0-or-later`):

- You may use, study, modify, and redistribute the project.
- Distribution of the project or derivative works must follow the complete GPL terms, including the corresponding-source and notice requirements.
- The software is provided without warranty; the complete GPL text governs warranty disclaimers and limitations of liability.
- `GPL-3.0-or-later` applies to versions distributed with the current `LICENSE`; licenses already granted for historical versions are not retroactively revoked.
- Third-party dependencies and external resources remain subject to their respective licenses.

See [LICENSE](LICENSE) for the complete, legally binding terms.
