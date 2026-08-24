<p align="center">
  <img src="assets/game-localizer-logo.png" alt="Game Localizer" width="720">
</p>

# Game Localizer

**简体中文** | [English](README.en.md)

Game Localizer 是一套面向游戏文本的本地化流水线，而不是单纯的机器翻译工具。它把资源扫描、翻译记忆、模型辅助翻译、质量检查、人工修订、制品构建和发布收敛到同一套可追踪流程中。

项目源于对《坦克世界》（Mir Tankov）俄服中文本地化的多年持续维护，并将真实版本更新中验证过的实践抽象为可复用框架。相关汉化资源与发布元数据维护在 [`tanki-i18n-metadata`](https://github.com/AllenXiao95/tanki-i18n-metadata)。

## 主要能力

- 支持 Gettext PO/MO、ParaTranz JSON 和 Paradox YAML。
- 使用稳定坐标和源文指纹管理 SQLite 翻译记忆。
- 支持 OpenAI-compatible 翻译模型、并发、限流和 tokenizer。
- 提供占位符、术语、源语言残留、过滤与规范化检查。
- 支持可审计的人工修订、失败恢复和增量重建。
- 构建 `preview` 与经过 QualityGate 的 `release` 制品。
- 发布到本地目录、GitHub Release、Cloudflare R2 和阿里云 OSS。

## 文档

完整使用说明已统一到文档站，README 只保留项目入口，避免两套文档内容漂移。

- [在线文档站](https://allenxiao95.github.io/Game-Localizer/)
- [仓库内文档首页](docs/index.md)
- [首次安装](docs/getting-started.md)
- [使用指南](docs/usage.md)
- [`project.yaml` 配置参考](docs/project-configuration.md)
- [TM 与 SQLite](docs/translation-memory.md)

## 最短启动路径

需要 Python 3.10 或更高版本。Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[all]"
localizer dashboard projects/example/project.yaml --host 127.0.0.1 --port 8080
```

然后访问 <http://127.0.0.1:8080>。项目配置、凭据、CLI 工作流、TM 迁移和发布说明以[文档站](https://allenxiao95.github.io/Game-Localizer/)为准。

## 开发

```powershell
python -X utf8 -m unittest discover -s tests
```

文档站使用开源的 [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/)。本地预览：

```powershell
python -m pip install -e ".[docs]"
mkdocs serve
```

## 当前边界

- 社区平台工作流目前只有配置模型和离线同步组件，尚无在线 API 客户端。
- Web 面板面向本机观测和单人定点修订，不是多人协作审批平台。
- 远端发布需要相应可选依赖与环境凭据；只有显式声明凭据轮换事件时才启用治理拦截。
- [M8 Agent 化工作流与 Tauri 客户端](docs/milestone-m8-agent-client.md)目前是设计提案。

## 许可证

本项目以自由软件形式按 [GNU General Public License v3.0 或任何后续版本](LICENSE)发布（SPDX：`GPL-3.0-or-later`）。第三方依赖和外部资源遵循各自的许可证；完整条款以 [LICENSE](LICENSE) 为准。
