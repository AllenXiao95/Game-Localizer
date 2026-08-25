<p align="center">
  <img src="assets/game-localizer-logo.png" alt="Game Localizer" width="720">
</p>

# Game Localizer

[简体中文](README.md) | **English**

Game Localizer is a localization pipeline for game text, not merely a machine-translation tool. It brings resource scanning, translation memory, model-assisted translation, quality assurance, human revision, artifact building, and publishing together in one traceable workflow.

The project was open-sourced in August 2026, but the workflow itself grew out of several years of maintaining Chinese localization for *Mir Tankov*. Practices validated through recurring real game updates were generalized into this reusable framework. Localization resources and release metadata are maintained separately in [`tanki-i18n-metadata`](https://github.com/AllenXiao95/tanki-i18n-metadata), keeping redistributable framework code distinct from game-specific content and release data.

Game Localizer does not normalize complete resource files into TM. Each resource format is projected by a `ResourceAdapter` into common `TranslationUnit` work units; TM persists translation knowledge and governance state only, and the owning Adapter writes resolved translations back against the original resource structure. See [Core Architecture and Adapter Contract](docs/en/core-architecture.md).

## Project status

Game Localizer is an actively developed pre-1.0 open-source project. The current repository already includes cross-platform CI, automated regression coverage, full-history secret scanning, bilingual documentation, an example project, and explicit QualityGate / human-authorization boundaries around release and publishing workflows.

The next design phase is organized as [M8: Quality Engineering, Controlled Intelligence, and Productization](docs/en/milestone-m8-agent-client.md). M8 is not a new localization kernel: quality engineering, optional Controlled Intelligence, and Tauri productization are independently validated tracks.

- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)
- [Issue tracker](https://github.com/AllenXiao95/Game-Localizer/issues)

## Key features

- Supports Gettext PO/MO, ParaTranz JSON, and Paradox YAML.
- Manages SQLite translation memory with stable coordinates and source fingerprints.
- Supports OpenAI-compatible translation models, concurrency, rate limiting, and tokenizers.
- Checks placeholders, terminology, residual source-language text, filtering, and normalization.
- Provides auditable human revision, failure recovery, and incremental rebuilds.
- Builds `preview` artifacts and QualityGate-protected `release` artifacts.
- Publishes to local directories, GitHub Releases, Cloudflare R2, and Alibaba Cloud OSS.

## Documentation

The complete usage documentation now lives in one documentation site. This README remains a project entry point so parallel instructions do not drift apart.

- [Documentation site](https://allenxiao95.github.io/Game-Localizer/en/)
- [Documentation source](docs/en/index.md)
- [Core Architecture and Adapter Contract](docs/en/core-architecture.md)
- [First-time setup](docs/en/getting-started.md)
- [Usage guide](docs/en/usage.md)
- [`project.yaml` reference](docs/en/project-configuration.md)
- [TM and SQLite](docs/en/translation-memory.md)

## Shortest path to a running dashboard

Python 3.10 or later is required. Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[all]"
localizer dashboard projects/example/project.yaml --host 127.0.0.1 --port 8080
```

Then open <http://127.0.0.1:8080>. The [documentation site](https://allenxiao95.github.io/Game-Localizer/en/) is authoritative for project configuration, credentials, CLI workflows, TM migration, and publishing.

## Development

```powershell
python -X utf8 -m unittest discover -s tests
```

The documentation site uses the open-source [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/). Preview it locally with:

```powershell
python -m pip install -e ".[docs]"
mkdocs serve
```

## Current limitations

- Community-platform workflows currently have a configuration model and offline synchronization components, but no online API client.
- The web dashboard targets local observability and targeted single-user revision, not multi-user approval workflows.
- Remote publishing requires the relevant optional dependencies and environment credentials; the governance gate activates only for an explicitly declared credential-rotation event.
- [M8: Quality Engineering, Controlled Intelligence, and Productization](docs/en/milestone-m8-agent-client.md) is currently a design proposal; its tracks are not all mandatory deliverables.

## License

This project is free software licensed under the [GNU General Public License v3.0 or later](LICENSE) (SPDX: `GPL-3.0-or-later`). Third-party dependencies and external resources retain their respective licenses. See [LICENSE](LICENSE) for the complete terms.
