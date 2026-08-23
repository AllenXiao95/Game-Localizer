# `project.yaml` reference

[中文](../project-configuration.md) | **English**

`project.yaml` defines what to translate, which rules/provider to use, and where results go. It does not store secrets or run state. The schema is strict: unknown, misspelled, and invalid field combinations fail `validate-config`. Relative paths are resolved from the directory containing the YAML file.

Start by copying [`projects/example/project.yaml`](https://github.com/AllenXiao95/Game-Localizer/blob/master/projects/example/project.yaml). Do not enable remote publishing, AES, or legacy migration until the minimal local workflow works.

## Readable minimal configuration

```yaml
schema_version: 1

project:
  id: my-project
  name: My Project
  game_version: 1.0.0

paths:
  source: ./source
  workspace: ../../var/my-project/workspace
  output: ../../var/my-project/output

languages:
  source: en-US
  target: zh-Hans

resources:
  adapters:
    - type: gettext
      include: ["**/*.po", "**/*.mo"]
      exclude: []
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
  database: ../../var/my-project/localizer.sqlite
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
      destination: ../../releases/my-project
      versioned_prefix: true
```

## Configuration reference

### `schema_version`

Must be `1`. This is the configuration format version, not the game version.

### `project`

| Field | Meaning |
| --- | --- |
| `id` | Stable machine identity and part of every TM coordinate. Changing it after TM accumulation prevents old coordinate hits. |
| `name` | Human-facing display name; not part of paths or TM identity. |
| `game_version` | Resource/game version used in artifacts and release identity. Use path-safe characters and omit a leading `v`. |

### `paths`

| Field | Meaning |
| --- | --- |
| `source` | Single source directory. Prefer this for simple projects. |
| `sources` | `variant: directory` mapping for production/test, channels, or platforms. Use instead of `source`. |
| `default_variant` | Variant selected when `--variant` is omitted. Multiple variants without a default require an explicit selection. |
| `workspace` | Checkpoints, run state, reports, and backups. |
| `output` | Rendered resources, preview/release artifacts, and manifests. |

At least one of `source` or `sources` is required. Variants get separate workspace/output subdirectories while sharing TM, glossary, and rules.

### `cache`

| Field | Default | Meaning |
| --- | --- | --- |
| `root` | `../../var/cache` | Regenerable cache root; tokenizer snapshots live below `tokenizers`. |
| `scope` | `shared` | Ownership label (`shared` or `project`). Actual path isolation is controlled by `root`; use a project-specific root with `project`. |

Never commit caches.

### `environment`

| Field | Default | Meaning |
| --- | --- | --- |
| `dotenv_files` | `[]` | Explicit `.env` files. |
| `auto_discover` | `false` | Search from the configuration directory toward the repository root. |
| `override_existing` | `false` | Whether `.env` replaces variables already present in the process. Keep `false` in most cases. |

Every `*_env` value must be an uppercase environment-variable name such as `LOCALIZER_API_KEY`, never a credential value.

### `languages`

`source` and `target` are locale identifiers such as `en-US` and `zh-Hans`. They drive TM metadata and language QA but do not automatically define an adapter's directory layout.

### `resources.adapters[]`

Common fields:

| Field | Default | Meaning |
| --- | --- | --- |
| `type` | none | Registered adapter: `gettext`, `paratranz_json`, or `paradox_yml`. |
| `include` | `["**/*"]` | Include globs relative to source root. |
| `exclude` | `[]` | Exclude backup, temporary, or non-translatable files. |
| `options` | `{}` | Strict adapter-specific options. |

Gettext options:

- `layout: standard` means `msgid` is source and `msgstr` translation; `keyed_source` means `msgid` is the stable key and `msgstr` source text.
- `empty_source` is `skip` or `error`.
- `source_filter: all` is the general choice. `cyrillic_without_cjk` is a legacy compatibility filter.

ParaTranz JSON maps `key_field`, `source_field`, `translation_field`, `context_field`, `stage_field`, and `id_field`; key/source/translation field names must differ.

Paradox YAML `locale_folders` maps locale identifiers to safe directory names.

### `prompt`

`template` contains target-language, style, naming, and formatting requirements. The pipeline adds batches and the output protocol. Optional `background` contains world/UI context, never secrets or rapidly changing run data. Keep terminology in the structured glossary rather than duplicating it in prompts.

### `glossary`

`file` points to glossary YAML. An empty glossary still has `schema_version: 1` and `terms: []`. `auto_discovery` is `candidate_only` (discoveries never auto-promote to mandatory terms) or `disabled`. Terms can specify exact/word matching, locales, review state, provenance, and path scope. See [`projects/example/glossary.yaml`](https://github.com/AllenXiao95/Game-Localizer/blob/master/projects/example/glossary.yaml).

### `rules`

`file` points to QA, filtering, and normalization rules. The minimum file is:

```yaml
schema_version: 1
```

Rules can define a source-language residue profile/allowlist, adapter/path/key/source filters, and scoped normalization regexes. Filtered entries are neither sent to the model nor included in QA. See [`projects/example/rules.yaml`](https://github.com/AllenXiao95/Game-Localizer/blob/master/projects/example/rules.yaml).

### `provider`

| Field | Default | Meaning |
| --- | --- | --- |
| `type` | `openai_compatible` | Currently the only provider type. |
| `base_url` | none | API root; `/chat/completions` is appended. |
| `api_key_env` | none | Environment variable containing the API key. |
| `model` | none | Server-side model ID. |
| `temperature` | `0.3` | Request temperature; translation usually benefits from a low value. |
| `timeout_seconds` | `120` | Per-request timeout. |
| `concurrency` | `4` | Maximum parallel resource-group workers. Final SQLite writes remain controlled. |
| `rpm` / `tpm` | empty | Reserved request/token limits. The current runner does not wire them into a limiter; do not treat them as active protection. |
| `context_window` | `32000` | Total model context used for local batching. |
| `max_output_tokens` | `4096` | Maximum output, and it must be smaller than `context_window`. |
| `custom_parameters` | `{}` | Vendor JSON parameters such as `top_p`. They cannot replace framework-owned request fields or contain credentials. |

Optional tokenizer:

```yaml
provider:
  tokenizer:
    type: huggingface
    model: organization/tokenizer-repository
    revision: pinned-revision
    local_files_only: false
```

Keeping this block requires `.[tokenizer-huggingface]`. Its model identity is independent of the provider model; removing it selects conservative estimation.

### `tm`

| Field | Default | Meaning |
| --- | --- | --- |
| `database` | none | SQLite path, created by the first write workflow. |
| `global_exact_match` | `reviewed_only` | Cross-coordinate source reuse: `disabled`, `reviewed_only`, or `reviewed_or_legacy_converged`. |
| `commit_policy` | `quality_gate` | The only current policy: machine results become formal after passing the gate. |

Variants of one project may share a database. Avoid casually sharing one database path between unrelated project IDs.

### `quality_gate`

Optional `legacy_debt_baseline` records known existing errors. Without it, release has zero tolerance for all errors. With it, new machine errors are still rejected; only explicitly registered existing debt is allowed.

### `workflow`

Use `mode: local` for normal projects. `paratranz` also requires `project_id` and `token_env` and can declare `minimum_release_stage` plus `sync.dry_run_by_default/delete_policy`. The repository currently contains its model and offline components but no online API client, so do not treat it as a ready online workflow.

### `build`

| Field | Default | Meaning |
| --- | --- | --- |
| `format` | `zip` | Currently only ZIP. |
| `release_channel` | `local-release` | Channel metadata such as stable/beta. |
| `variant` | empty | Public artifact variant, distinct from a `paths.sources` key. |
| `artifact_prefix` | `i18n` | Artifact filename prefix. |
| `compression` | `deflate` | `deflate`, `lzma`, or `stored`; deflate is most compatible. |
| `encryption` | `none` | `none` or `aes256`; AES requires `.[artifact-aes]`. |
| `password_env` | empty | Environment-variable name for the AES password; valid only with AES. |
| `archive_root` | empty | Safe relative root prepended inside the archive. |

`compatibility_metadata` supports legacy `metadata.json` consumers through `enabled`, fixed format `legacy_v6`, `filename`, and `env`. Leave it disabled for general new projects.

`variant_overrides` maps source variants to public release identity:

```yaml
build:
  variant_overrides:
    stable:
      variant: desktop
      compatibility_env: DESKTOP
```

Every mapping key must be declared in `paths.sources`.

### `review`

`decisions_file` is the base path for append-only monthly human-decision logs. `reviewer` is the Dashboard display identity and defaults to the OS user; it is not authentication.

### `security`

`credential_rotation_completed_at` and `rotation_record` attest remote-publishing credential governance. Both are required when any remote target exists; local publishing does not need them. Never insert fake values merely to pass validation.

### `publish.targets[]`

Common fields include `type`, `prefix`, `versioned_prefix`, and remote `timeout_seconds`.

| Type | Required fields | Extra |
| --- | --- | --- |
| `local` | `destination` | none |
| `github_release` | `repository`, `token_env`; optional `tag` | none |
| `cloudflare_r2` | `bucket`, access/secret env names, and `account_id` or `endpoint_url` | `publish-r2` |
| `alibaba_oss` | `endpoint`, `bucket`, `sts_token_url`, `sts_token_env`; optional header | `publish-oss` |

Credential fields always contain environment-variable names.

## Validation order after changes

```powershell
localizer validate-config projects/my-project/project.yaml
localizer scan projects/my-project/project.yaml
localizer dashboard projects/my-project/project.yaml --host 127.0.0.1 --port 8765
```

This separates YAML/schema errors, resource matching errors, and runtime dependency errors.
