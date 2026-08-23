---
hide:
  - toc
---

# Game Localizer 文档

Game Localizer 是一套可追踪、可审计的游戏文本本地化流水线，覆盖资源扫描、翻译记忆、模型辅助翻译、质量检查、人工修订、制品构建与发布。

[开始安装](getting-started.md){ .md-button .md-button--primary }
[查看使用流程](usage.md){ .md-button }

## 推荐阅读顺序

1. [首次安装与第一次运行](getting-started.md)
2. [通用使用指南](usage.md)
3. [`project.yaml` 配置说明](project-configuration.md)
4. [从存量汉化资源构建 TM](tm-bootstrap.md)
5. [TM 与 SQLite 指南](translation-memory.md)

## 按问题查找

| 问题 | 文档 |
| --- | --- |
| 如何选择 venv、Conda 和安装功能集？ | [首次安装](getting-started.md) |
| 如何扫描、预览、复核、构建和发布？ | [使用指南](usage.md) |
| 某个 YAML 字段是什么意思？ | [项目配置](project-configuration.md) |
| 只有已有汉化资源，没有 TM 怎么办？ | [从存量资源构建 TM](tm-bootstrap.md) |
| 如何初始化、迁移或核验 SQLite TM？ | [TM 与 SQLite](translation-memory.md) |

## English

The complete English documentation starts at the [English overview](en/index.md).

## 设计提案

[M8 Agent 化工作流与 Tauri 客户端设计](milestone-m8-agent-client.md)记录了下一阶段的规划范围。它是设计提案，不代表相关能力已经交付。
