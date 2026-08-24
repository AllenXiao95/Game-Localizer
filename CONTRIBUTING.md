# Contributing to Game Localizer

Thanks for helping improve Game Localizer. The project aims to keep game-localization workflows reproducible, reviewable, and safe to operate across real release cycles.

## Before opening a change

- Search existing issues and pull requests first.
- Keep changes focused. Avoid mixing unrelated refactors, documentation work, and behavior changes in one pull request.
- For user-visible behavior changes, describe the affected workflow and expected outcome.
- For parser, translation-memory, QA, build, or publishing changes, include regression coverage where practical.

## Development setup

Python 3.10 or later is required.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[all]"
python -X utf8 -m unittest discover -s tests
```

Documentation changes can be checked with:

```powershell
python -m pip install -e ".[docs]"
mkdocs build --strict
```

## Pull requests

A good pull request should include:

1. A concise description of the problem and the intended behavior.
2. The scope of the change and any compatibility implications.
3. Tests or validation steps.
4. Documentation updates when configuration or user-facing workflows change.
5. No secrets, credentials, copyrighted game assets, or generated artifacts that should not be redistributed.

For changes touching release governance, QualityGate, translation-memory writes, remote publishing, or security boundaries, explain how deterministic validation and human authorization remain enforced.

## Issues

Bug reports are most useful when they include:

- operating system and Python version;
- the Game Localizer version or commit SHA;
- the relevant resource format;
- a minimal reproducible input when redistribution is permitted;
- the exact command or workflow step;
- expected and actual behavior;
- relevant logs with secrets removed.

Feature requests should describe the localization workflow or maintainer problem being solved, not only a preferred implementation.

## Project direction

The project prioritizes:

- deterministic QA and release safety;
- traceable translation-memory and human-review workflows;
- format adapters and reusable localization infrastructure;
- recovery, reproducibility, and maintainability;
- controlled use of model-assisted and agentic workflows.

Large architectural changes should start with an issue or design discussion before implementation.

## License

By contributing, you agree that your contributions are licensed under the repository's GPL-3.0-or-later license.