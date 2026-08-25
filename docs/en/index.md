---
hide:
  - toc
---

# Game Localizer documentation

Game Localizer is a traceable and auditable localization pipeline for game text. It covers resource scanning, translation memory, model-assisted translation, quality assurance, human revision, artifact building, and publishing.

[Get started](getting-started.md){ .md-button .md-button--primary }
[View the workflow](usage.md){ .md-button }

## Recommended reading order

1. [First-time setup](getting-started.md)
2. [General usage guide](usage.md)
3. [`project.yaml` reference](project-configuration.md)
4. [Core Architecture and Adapter Contract](core-architecture.md)
5. [Build TM from existing translations](tm-bootstrap.md)
6. [TM and SQLite guide](translation-memory.md)

## Find documentation by question

| Question | Documentation |
| --- | --- |
| How should I choose between venv, Conda, and installation profiles? | [First-time setup](getting-started.md) |
| How do I scan, preview, review, build, and publish? | [Usage guide](usage.md) |
| What does a YAML field mean? | [Project configuration](project-configuration.md) |
| How do Adapter, TranslationUnit, TM, and original resources relate? | [Core Architecture and Adapter Contract](core-architecture.md) |
| What if translated resources exist but no TM does? | [Build TM from existing resources](tm-bootstrap.md) |
| How do I initialize, migrate, or verify a SQLite TM? | [TM and SQLite](translation-memory.md) |

## 中文

完整中文文档从[中文首页](../index.md)开始。

## Design proposal

[M8: Quality Engineering, Controlled Intelligence, and Productization](milestone-m8-agent-client.md) records next-stage hypotheses and priorities. Its tracks may stop or proceed independently and are not statements that those capabilities have shipped.
