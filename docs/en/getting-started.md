# First-time setup

[中文](../getting-started.md) | **English**

## Prerequisites

- Python 3.10 or later
- Git
- An OpenAI Chat Completions-compatible translation endpoint; configuration validation and scanning do not require an API key

Commands below run from the repository root. PowerShell is used for Windows examples.

## Create an isolated environment

Choose either venv or Conda. Do not install project dependencies into the system Python.

### venv

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

Use `deactivate` to leave the environment.

### Conda

```powershell
conda env create -f environment.yml
conda activate game_localizer
```

`environment.yml` creates the Python environment only. Install the current project in the next step so local source changes take effect immediately.

## Choose an installation profile

Recommended first-time installation:

```powershell
python -m pip install -e ".[all]"
```

`-e` is an editable install. The `all` extra contains:

| Capability | Extra | Required when |
| --- | --- | --- |
| Hugging Face local tokenizer | `tokenizer-huggingface` | `provider.tokenizer` is configured |
| AES-256 archives | `artifact-aes` | `build.encryption: aes256` |
| Cloudflare R2 and Alibaba OSS | `publish-all` | Those remote targets are used |

For local builds without a configured tokenizer, a minimal installation is enough:

```powershell
python -m pip install -e .
```

Install one optional capability later with, for example:

```powershell
python -m pip install -e ".[tokenizer-huggingface]"
```

### Is the tokenizer mandatory?

It is conditionally mandatory, not a core unconditional dependency:

- If `provider.tokenizer` exists, `transformers` must be installed. The configured tokenizer is loaded before analysis/translation so an incorrect token budget cannot silently be used.
- If that block is absent, the built-in conservative estimator is used and `transformers` is unnecessary.

This error means the configuration enabled an optional capability whose dependency is missing:

```text
provider.tokenizer is configured but transformers is not installed
```

Install `.[tokenizer-huggingface]` or remove the entire tokenizer block. Do not remove only its `model` field.

For a Hugging Face tokenizer:

- `model` identifies a tokenizer repository or local snapshot; it is never inferred from `provider.model`.
- Pin `revision` for reproducible token splitting.
- `local_files_only: true` requires a complete cached snapshot. Use `false` for the first online download.
- Snapshots are stored below `cache.root/tokenizers`.

## Verify the installation

```powershell
localizer --help
localizer validate-config projects/example/project.yaml
localizer scan projects/example/project.yaml
```

The last two commands do not call the translation API. Values such as API URL, model, and tokenizer in the example are placeholders; replace them before analysis or build, or remove the tokenizer block when exact counting is unnecessary.

## Create a project

Copy the whole example directory:

```powershell
Copy-Item -Recurse projects\example projects\my-project
```

At minimum, change:

1. `project.id`, `project.name`, and `project.game_version`.
2. `paths.source`.
3. `languages.source` and `languages.target`.
4. `resources.adapters` for the actual file format.
5. `provider.base_url`, `provider.model`, and `provider.api_key_env`.
6. `tm.database`, `paths.workspace`, and `paths.output` so projects cannot overwrite each other.

Every relative path is resolved from the directory containing `project.yaml`, not the terminal working directory.

## Configure credentials

YAML stores only the environment-variable name:

```yaml
provider:
  api_key_env: LOCALIZER_API_KEY
```

PowerShell:

```powershell
$env:LOCALIZER_API_KEY = "your-api-key"
```

Or use a Git-ignored `.env`:

```dotenv
LOCALIZER_API_KEY=your-api-key
```

```yaml
environment:
  auto_discover: true
  override_existing: false
```

Keeping `override_existing: false` lets explicit shell/CI variables override `.env`.

## First run

```powershell
localizer validate-config projects/my-project/project.yaml
localizer scan projects/my-project/project.yaml
localizer build projects/my-project/project.yaml --mode preview --run-id first-preview
```

`build` automatically creates the SQLite parent directory, file, schema, and indexes. No manual SQL is required. Preview output is for inspection and is not presented as a publishable release.

Dashboard alternative:

```powershell
localizer dashboard projects/my-project/project.yaml --host 127.0.0.1 --port 8765
```

Open <http://127.0.0.1:8765>. Write operations are enabled only on a loopback bind.

## Common startup problems

### PowerShell blocks activation

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

Or call `.\.venv\Scripts\python.exe` directly without activation.

### `localizer` is not found

Activate the intended environment and rerun:

```powershell
python -m pip install -e ".[all]"
python -m pip show game-localizer
```

### Tokenizer download fails

Verify `provider.tokenizer.model` and `revision`. In an offline environment, pre-populate `cache.root/tokenizers` and set `local_files_only: true`; otherwise remove the tokenizer block to use conservative estimation.

### The scan is empty

Check `paths.source`, adapter `include`/`exclude` globs, and that the file format matches `type`. Diagnose with `localizer scan` before running a build.
