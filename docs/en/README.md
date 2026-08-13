# Game Localizer documentation

[中文文档](../README.md) | **English documentation**

This documentation is for first-time users. It generalizes lessons from the earlier single-game script workflow without depending on game-specific install paths, filenames, or publishing services.

Read in this order:

1. [First-time setup](getting-started.md)
2. [General usage guide](usage.md)
3. [`project.yaml` reference](project-configuration.md)
4. [Build TM from existing translations](tm-bootstrap.md)
5. [TM and SQLite guide](translation-memory.md)

Common questions:

- venv or Conda: [Create an isolated environment](getting-started.md#create-an-isolated-environment).
- `transformers is not installed`: [Choose an installation profile](getting-started.md#choose-an-installation-profile).
- Whether SQLite tables must be created manually: [Initialize a new SQLite TM](translation-memory.md#initialize-a-new-sqlite-tm).
- Convert legacy TM JSON to SQLite: [Convert legacy JSON TM to SQLite](translation-memory.md#convert-legacy-json-tm-to-sqlite).
- Only translated resources are available: [Build TM from existing translations](tm-bootstrap.md).
- Understand a YAML field: [Configuration reference](project-configuration.md#configuration-reference).
