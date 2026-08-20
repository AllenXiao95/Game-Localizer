<p align="center">
  <img src="assets/game-localizer-logo.png" alt="Game Localizer" width="720">
</p>

# Game Localizer

**简体中文** | [English](README.en.md)

Game Localizer 是一个面向游戏文本的本地化流水线，而不是机器翻译工具。它把资源扫描、翻译记忆、模型辅助翻译、质量检查、人工修订、制品构建和发布收敛到同一套可追踪流程中。

项目以声明式配置驱动，适合单个游戏、多资源目录和多版本持续更新。默认使用 SQLite 作为翻译记忆（TM）的权威数据源。

## 生产实践背景

Game Localizer 源于对《坦克世界》（Mir Tankov）俄服中文本地化的多年持续维护；相关汉化资源与发布元数据公开维护在 [`tanki-i18n-metadata`](https://github.com/AllenXiao95/tanki-i18n-metadata)。本框架将经过真实游戏版本更新验证的资源处理、质量检查和制品发布实践抽象为可复用流水线，以服务更多游戏和社区汉化项目。

## 主要能力

- 支持 Gettext PO/MO、ParaTranz JSON 和 Paradox YAML 资源。
- 通过 OpenAI-compatible 接口接入翻译模型，并支持并发、限流和本地 tokenizer。
- 以稳定坐标和源文指纹管理 SQLite TM，避免源文变化后误用旧译文。
- 区分模型生成译文、人工定稿和历史迁移记录，保护已审核内容。
- 提供占位符、源语言残留、术语、过滤和规范化检查。
- 支持 `preview` 与 `release` 两种构建模式；正式制品必须通过 QualityGate。
- 保存运行状态、检查点、报告和清单，可恢复失败运行或从父运行增量重建。
- 提供本地观测面板，用于查看运行、定位 QA 问题并提交可审计的人工修订。
- 支持发布到本地目录、GitHub Release、Cloudflare R2 和阿里云 OSS。

## 环境要求

- Python 3.10 或更高版本
- Git（用于版本管理；运行流水线本身不强制依赖远端仓库）

第一次在本地使用时，建议先创建隔离环境，再安装完整功能集。Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[all]"
```

也可以使用 Conda：

```powershell
conda env create -f environment.yml
conda activate game_localizer
python -m pip install -e ".[all]"
```

`.[all]` 是首次使用的推荐选项，包含 Hugging Face tokenizer、AES 制品和全部远端发布适配器。如果希望保持最小安装，可使用 `python -m pip install -e .`，再按需安装：

```powershell
# AES-256 加密制品
python -m pip install -e ".[artifact-aes]"

# Hugging Face tokenizer
python -m pip install -e ".[tokenizer-huggingface]"

# 全部远端发布适配器
python -m pip install -e ".[publish-all]"
```

`tokenizer-huggingface` 是“条件必选项”：只要 `project.yaml` 中保留了 `provider.tokenizer`，运行分析或翻译前就必须安装它；删除整个 `tokenizer` 配置块后，程序会改用无需额外依赖的保守 token 估算器。详见[首次安装](docs/getting-started.md#选择安装方式)。

## 文档

- [首次安装与第一次运行](docs/getting-started.md)：venv/Conda、安装选项、配置凭据和首轮验证。
- [通用使用指南](docs/usage.md)：从资源扫描、预览、人工复核到正式构建和发布。
- [`project.yaml` 配置说明](docs/project-configuration.md)：按语义解释所有配置段、常见取值和适用场景。
- [从存量汉化资源构建 TM](docs/tm-bootstrap.md)：直接读取受支持资源，或把未知结构转换成中立 TM Seed。
- [TM 与 SQLite 指南](docs/translation-memory.md)：新建 TM、旧 JSON 转 SQLite、核验、权威源切换与回滚。

## 快速开始

### 1. 启动 Dashboard（推荐）

Dashboard 是日常工作的首选入口，用于启动本地任务、查看运行状态、定位 QA 问题、提交人工修订和发起增量重建。安装后可直接使用仓库自带示例启动：

```powershell
localizer dashboard projects/example/project.yaml --host 127.0.0.1 --port 8080
```

然后访问 <http://127.0.0.1:8080>。Dashboard 可以在未配置 API 密钥时打开，但执行模型辅助翻译前仍需完成下面的项目配置与凭据设置。写操作仅在回环地址启用。

### 2. 准备项目目录

建议把每个游戏的配置和规则放在独立目录中：

```text
projects/my-game/
├── project.yaml
├── prompt.md
├── background.md
├── glossary.yaml
└── rules.yaml
```

最小的 `project.yaml` 示例：

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

配置中的相对路径以 `project.yaml` 所在目录为基准。凭据字段只接受环境变量名，不要把密钥写入 YAML。

准备辅助文件：

```yaml
# glossary.yaml
schema_version: 1
terms: []
```

```yaml
# rules.yaml
schema_version: 1
```

`prompt.md` 应说明目标语言、文风、格式约束，并要求模型保留输入中的占位符。可选的 `background.md` 用于补充游戏世界观和 UI 语境。模型响应的具体批次格式由流水线负责组装。

完整且带注释的配置、术语、规则和资源案例位于 [`projects/example`](projects/example/project.yaml)。

### 3. 设置凭据

PowerShell：

```powershell
$env:LOCALIZER_API_KEY = "your-api-key"
```

也可以使用 `.env`，并在配置的 `environment` 段启用自动发现。`.env` 必须保持在版本控制之外。

### 4. 校验并扫描

```powershell
localizer validate-config projects/my-game/project.yaml
localizer scan projects/my-game/project.yaml
```

### 5. 使用 CLI 构建预览（可选）

```powershell
localizer build projects/my-game/project.yaml --mode preview --run-id preview-001
```

预览运行会生成输出、QA 报告和运行记录，但不会伪装成可发布的正式制品。

Dashboard 可直接启动预览任务；以下 CLI 命令适合脚本、CI 和无人值守运行。

### 6. 使用 CLI 构建与发布正式制品

```powershell
localizer build projects/my-game/project.yaml --mode release --run-id release-001
localizer publish projects/my-game/project.yaml path/to/artifact-manifest.json
```

`release` 会执行完整质量闸门。只有通过闸门的清单才能进入发布流程。

## 常用工作流

### Dashboard 日常工作

```powershell
localizer dashboard projects/my-game/project.yaml --host 127.0.0.1 --port 8765
```

面板中的人工修订会记录操作者与 append-only 决策日志，并同步到正式 TM。配置文件本身仍由版本控制管理；Dashboard 负责运行、观测和受控修订。

### 从父运行增量重建

适合正式检查失败后已经人工修复、又不希望重新请求模型生成全部译文的场景：

```powershell
localizer rebuild-from-run projects/my-game/project.yaml `
  --parent-run-id release-001 `
  --run-id release-002 `
  --mode release
```

该命令会校验源文指纹，复用父运行中仍然有效的结果，只重新处理未解决条目。

### 多资源变体

项目可以在 `paths.sources` 中声明多个资源目录，并使用 `paths.default_variant` 或 CLI 的 `--variant` 选择。Dashboard 可在页面的“资源环境”下拉框直接切换；GET API 使用 `?variant=<name>`，写 API、预检、任务快照和任务预设使用同名 `variant` 字段。各变体拥有独立运行目录和输出目录，但共享同一个 TM、术语表与规则集，所有变体任务仍进入同一个串行队列。

```powershell
localizer build projects/my-game/project.yaml --variant beta --mode preview --run-id beta-001
```

需要让公开包名和发布目录随资源变体切换时，可配置：

```yaml
build:
  variant_overrides:
    stable: {variant: ru, compatibility_env: RU}
    beta: {variant: pt, compatibility_env: PT}
```

该映射同步控制制品名、release slug、版本化上传目录和兼容 `metadata.json` 的 `env`。

### 仅复制已有正式制品

```powershell
localizer publish-local path/to/artifact-manifest.json path/to/destination
```

## 翻译记忆（TM）

SQLite 是运行时和人工修订的权威源。正式写入遵循以下原则：

- 坐标由项目、适配器、相对路径和逻辑键共同确定。
- 查询同时核对源文指纹；源文改变时旧译文不会直接命中。
- 人工审核记录的优先级高于机器结果，批量操作不能静默覆盖。
- 机器译文只有通过质量闸门后才能提交为正式记录。
- 每次变更保留来源、状态、运行标识和审计信息。

新项目不需要手工执行 SQL 或预建数据库：`tm.database` 的父目录和 SQLite 表会在第一次运行 `localizer build` 等写流程时自动创建。创建数据库结构不等于机器候选已经成为正式记录；正式写入仍受构建模式和质量闸门约束。完整流程和检查命令见 [TM 与 SQLite 指南](docs/translation-memory.md)。

如果只有一批已经汉化的资源、没有旧 TM，可以直接预检受支持格式，或导入转换后的中立 Seed：

```powershell
# 从 project.yaml 中 adapter 能读取的已有译文生成报告（默认不写库）
localizer tm-bootstrap-resources projects/my-game/project.yaml

# 未知结构先转换成 TM Seed；可一次传入多个文件（默认不写库）
localizer tm-import-seed projects/my-game/project.yaml path/to/ui-seed.json path/to/items-seed.json
```

报告确认无误后增加 `--apply --accepted-by <操作者>`。具体格式、单文件与多文件示例见[从存量汉化资源构建 TM](docs/tm-bootstrap.md)。

从旧系统迁移时，先同步和核验，再进行权威源切换：

```powershell
localizer tm-sync-legacy projects/my-game/project.yaml path/to/legacy-tm.json

# 默认只生成差异报告
localizer tm-adopt-artifact projects/my-game/project.yaml path/to/artifact-manifest.json

# 经人工确认后写入 SQLite
localizer tm-adopt-artifact projects/my-game/project.yaml path/to/artifact-manifest.json `
  --apply `
  --accepted-by project-owner

localizer tm-verify-artifact projects/my-game/project.yaml path/to/artifact-manifest.json `
  --run-id verify-001
```

`tm-switch-authority` 是治理动作，不是普通构建步骤。执行前必须保存行为基线、数据基线和旧 TM，并确认回滚与审计材料齐全。

## 目录与产物

典型运行数据位于配置指定的 `workspace` 与 `output`：

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

运行目录属于可再生成数据，正式制品及其 manifest 才是发布和回读验证的入口。不要只凭压缩包文件名判断版本、模式或质量状态。

## 发布与安全

- 本地发布不需要凭据治理声明。
- 远端发布默认关闭；完成凭据轮换并配置审计记录后才会放行。
- 配置文件只能引用环境变量名，不能保存 token、密码或访问密钥。
- 每个发布目标独立执行；单个目标失败不会破坏本地制品，并会返回可重试结果。
- 正式发布应固定版本、保留 manifest，并对上传结果执行回读验证。

## 测试

运行完整测试集：

```powershell
python -X utf8 -m unittest discover -s tests
```

提交前也建议运行：

```powershell
python -m pre_commit run --all-files
```

## 示例项目

- [最小可运行配置](projects/example/project.yaml)
- [示例游戏背景](projects/example/background.md)
- [示例翻译提示词](projects/example/prompt.md)
- [示例术语表](projects/example/glossary.yaml)
- [示例规则](projects/example/rules.yaml)
- [示例 Gettext 资源](projects/example/source/messages.po)

## 当前边界

- 社区平台工作流的配置模型与离线同步组件已存在，但在线 API 客户端尚未实现。
- Web 面板用于本机观测和单人定点修订，不是带认证、任务分配和多人审批的协作平台。
- 远端发布依赖相应的可选依赖、环境凭据和显式治理配置。

## 许可证

本项目以自由软件形式按 [GNU General Public License v3.0 或任何后续版本](LICENSE)发布（SPDX：`GPL-3.0-or-later`）：

- 你可以使用、研究、修改和再分发本项目。
- 分发本项目或其衍生作品时，必须遵守 GPL 的完整条款，包括提供相应源代码并保留版权与许可声明。
- 本软件不提供任何担保；责任限制以 GPL 正文为准。
- `GPL-3.0-or-later` 适用于随当前 `LICENSE` 分发的版本；历史版本已经取得的许可不会被追溯撤销。
- 第三方依赖和外部资源继续遵循各自的许可证。

完整、具有约束力的条款以 [LICENSE](LICENSE) 为准。
