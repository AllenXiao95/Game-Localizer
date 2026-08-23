# `project.yaml` 配置说明

**中文** | [English](en/project-configuration.md)

## 阅读方式

`project.yaml` 描述“翻译什么、用什么规则和模型、把结果放哪里”。它不保存 API 密钥，也不保存运行状态。

配置使用严格 schema：未知字段、拼错字段和不合法组合会在 `validate-config` 阶段报错。所有相对路径均以 `project.yaml` 所在目录为基准。

先从 [`projects/example/project.yaml`](https://github.com/AllenXiao95/Game-Localizer/blob/master/projects/example/project.yaml) 复制，再按本文修改。不要一开始就启用远端发布、AES 或旧系统迁移等高级能力。

## 一份可读的最小配置

```yaml
schema_version: 1

project:
  id: my-project             # 稳定机器标识；建库后不要随意修改
  name: My Project           # 展示名称
  game_version: 1.0.0        # 游戏/资源版本；不要写开头的 v

paths:
  source: ./source           # 原始资源，只读输入
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

## 配置字典

### `schema_version`

| 字段 | 必填 | 语义 |
| --- | --- | --- |
| `schema_version` | 是 | 当前只能为 `1`。它是配置格式版本，不是游戏版本。 |

### `project`

| 字段 | 必填 | 语义 |
| --- | --- | --- |
| `id` | 是 | 项目的稳定 ID，参与 TM 坐标。开始积累 TM 后修改它会让旧记录无法按原坐标命中。 |
| `name` | 是 | 面向人的展示名称，不参与路径和 TM 身份计算。 |
| `game_version` | 是 | 本轮资源/游戏版本，进入制品元数据和发布身份。允许字母、数字、点、横线、下划线；不要以 `v` 开头。 |

### `paths`

| 字段 | 必填 | 语义 |
| --- | --- | --- |
| `source` | 二选一 | 单资源目录。简单项目优先使用。 |
| `sources` | 二选一 | `变体名: 目录` 映射，用于正式/测试、渠道或平台等并行资源。 |
| `default_variant` | 否 | 未传 `--variant` 时默认选择的 `sources` 键。多个变体且无默认值时必须在命令行选择。 |
| `workspace` | 是 | checkpoint、运行状态、报告、备份等中间数据的根目录。 |
| `output` | 是 | 渲染后的资源、preview/release 制品及 manifest 的根目录。 |

`source` 与 `sources` 至少提供一个。使用 `sources` 后，各变体的 workspace/output 自动放到同名子目录，但 TM、术语和规则仍共享。

### `cache`

| 字段 | 默认值 | 语义 |
| --- | --- | --- |
| `root` | `../../var/cache` | 可再生成缓存的根目录；Hugging Face 快照放在其 `tokenizers` 子目录。 |
| `scope` | `shared` | 缓存归属提示，可为 `shared` 或 `project`。当前路径隔离仍由 `root` 决定；选择 `project` 时应同时给该项目独立的 `root`。 |

缓存不应提交到 Git。

### `environment`

| 字段 | 默认值 | 语义 |
| --- | --- | --- |
| `dotenv_files` | `[]` | 显式加载的 `.env` 文件列表。 |
| `auto_discover` | `false` | 从配置目录向仓库根方向查找 `.env`。 |
| `override_existing` | `false` | `.env` 是否覆盖进程中已存在的环境变量。建议保持 `false`。 |

YAML 中所有 `*_env` 字段都只能写大写环境变量名，例如 `LOCALIZER_API_KEY`，不能直接写密钥。

### `languages`

| 字段 | 必填 | 语义 |
| --- | --- | --- |
| `source` | 是 | 源语言 locale，例如 `en-US`。用于 TM 元数据和语言规则选择。 |
| `target` | 是 | 目标语言 locale，例如 `zh-Hans`。 |

locale 是项目语义，不会自动决定某个资源适配器的目录布局；特殊目录映射在 adapter `options` 中配置。

### `resources.adapters[]`

通用字段：

| 字段 | 默认值 | 语义 |
| --- | --- | --- |
| `type` | 无 | 已注册适配器名：`gettext`、`paratranz_json` 或 `paradox_yml`。 |
| `include` | `["**/*"]` | 相对于 source 的包含 glob。 |
| `exclude` | `[]` | 排除 glob，适合忽略备份、临时或不应翻译的文件。 |
| `options` | `{}` | 适配器专属选项，未知字段同样会报错。 |

Gettext 选项：

| 字段 | 默认值 | 语义 |
| --- | --- | --- |
| `layout` | `standard` | `standard` 表示 `msgid` 是源文、`msgstr` 是译文；`keyed_source` 表示 `msgid` 是稳定键、`msgstr` 是待翻译源文。 |
| `empty_source` | `skip` | 源文为空时跳过；设为 `error` 则把它当配置/数据错误。 |
| `source_filter` | `all` | `all` 处理所有源文；`cyrillic_without_cjk` 是旧兼容过滤策略，通用项目通常不应启用。 |

ParaTranz JSON 选项用于映射 `key_field`、`source_field`、`translation_field`、`context_field`、`stage_field`、`id_field`。前三个字段名必须互不相同。

Paradox YAML 的 `locale_folders` 是 `locale: 安全目录名` 映射，用于项目目录名与标准 locale 不一致的情况。

### `prompt`

| 字段 | 必填 | 语义 |
| --- | --- | --- |
| `template` | 是 | 项目级翻译要求：目标语言、语气、专名和格式约束。流水线会在其外部组装批次数据。 |
| `background` | 否 | 世界观、UI 场景、角色关系等背景。不要放凭据或会频繁变化的运行数据。 |

提示词应明确要求保留占位符，但不要在这里复制整份术语表；术语应由 `glossary.file` 结构化维护。

### `glossary`

| 字段 | 默认值 | 语义 |
| --- | --- | --- |
| `file` | 无 | 术语 YAML 路径。即使没有术语，也应提供 `schema_version: 1` 和空 `terms`。 |
| `auto_discovery` | `candidate_only` | `candidate_only` 表示自动发现结果只能作为候选，不能自动晋升为强制术语；`disabled` 关闭候选发现策略。 |

术语自身可声明 exact/word 匹配、locale、审核状态、来源及可选路径 scope。参考 [`projects/example/glossary.yaml`](https://github.com/AllenXiao95/Game-Localizer/blob/master/projects/example/glossary.yaml)。

### `rules`

`file` 指向 QA、过滤和规范化规则。空规则文件至少包含：

```yaml
schema_version: 1
```

可配置源语言残留画像、允许保留词、按 adapter/path/key/source 过滤的条目，以及按 adapter/path 生效的规范化正则。参考 [`projects/example/rules.yaml`](https://github.com/AllenXiao95/Game-Localizer/blob/master/projects/example/rules.yaml)。过滤规则会让命中项不送模型也不参与 QA，应谨慎使用。

### `provider`

| 字段 | 默认值 | 语义 |
| --- | --- | --- |
| `type` | `openai_compatible` | 当前唯一 provider 类型。 |
| `base_url` | 无 | API 根地址，程序会追加 `/chat/completions`。不要把完整的该端点重复写两次。 |
| `api_key_env` | 无 | API 密钥所在的环境变量名。 |
| `model` | 无 | 服务端模型 ID。 |
| `temperature` | `0.3` | 请求温度。稳定翻译通常保持较低。 |
| `timeout_seconds` | `120` | 单次请求超时秒数。 |
| `concurrency` | `4` | 并行处理资源组的最大 worker 数；同一 SQLite 的最终写入仍受控。 |
| `rpm` / `tpm` | 空 | 预留的请求/Token 限额字段。当前 runner 尚未把它们接入限流器，不能把填写它们当作已生效的供应商限流保护。 |
| `context_window` | `32000` | 模型总上下文窗口，用于本地组批。 |
| `max_output_tokens` | `4096` | 每次请求最大输出 token，必须小于 `context_window`。 |
| `custom_parameters` | `{}` | 合并到请求 JSON 的供应商扩展参数，例如 `top_p`。不能覆盖 model/messages/max_tokens/temperature 等框架字段，也不能包含凭据。 |

可选 tokenizer：

```yaml
provider:
  tokenizer:
    type: huggingface
    model: organization/tokenizer-repository
    revision: pinned-revision
    local_files_only: false
```

保留这个块就必须安装 `.[tokenizer-huggingface]`。`model` 是 tokenizer 身份，与上面的服务端 `model` 独立；删除整个块则使用保守估算器。

### `tm`

| 字段 | 默认值 | 语义 |
| --- | --- | --- |
| `database` | 无 | SQLite 文件路径。首次写流程自动创建。 |
| `global_exact_match` | `reviewed_only` | 跨坐标相同源文复用策略。`disabled` 禁用；`reviewed_only` 只复用人工审核记录；`reviewed_or_legacy_converged` 还允许已收敛的旧记录。 |
| `commit_policy` | `quality_gate` | 当前唯一策略：机器结果通过质量闸门后才成为正式 TM。 |

同一个 SQLite 可以给同项目的多个资源变体使用，但不建议让不同 `project.id` 共用同一条无治理的数据库路径。

### `quality_gate`

`legacy_debt_baseline` 是可选的历史问题基线文件。未配置时，release 对所有新增 error 零容忍。配置后也不会放过本次机器翻译产生的新 error，只允许明确登记的存量债务暂时存在。

### `workflow`

本地项目使用：

```yaml
workflow:
  mode: local
```

`paratranz` 模式还要求 `project_id`、`token_env`，可声明 `minimum_release_stage` 和 `sync.dry_run_by_default/delete_policy`。当前仓库只有配置模型和离线同步组件，在线 API 客户端尚未实现，因此新项目不要把它当作可直接运行的在线工作流。

### `build`

| 字段 | 默认值 | 语义 |
| --- | --- | --- |
| `format` | `zip` | 当前只支持 zip。 |
| `release_channel` | `local-release` | 写入构建元数据的发布通道名，如 stable/beta。 |
| `variant` | 空 | 公开制品身份中的变体名；与 `paths.sources` 的资源变体不是同一概念。 |
| `artifact_prefix` | `i18n` | 制品文件名前缀。 |
| `compression` | `deflate` | `deflate`、`lzma` 或 `stored`。兼容优先选 deflate。 |
| `encryption` | `none` | `none` 或 `aes256`。AES 需要 `.[artifact-aes]`。 |
| `password_env` | 空 | AES 密码的环境变量名；仅在 `aes256` 时允许。 |
| `archive_root` | 空 | 压缩包内部统一增加的安全相对路径前缀。 |

`compatibility_metadata` 用于需要旧版 `metadata.json` 的下游：`enabled` 开关、`format` 当前只能是 `legacy_v6`、`filename` 是包内文件名、`env` 是下游环境标识。通用新项目保持关闭。

`variant_overrides` 把 `paths.sources` 的资源变体映射为公开发布身份：

```yaml
build:
  variant_overrides:
    stable:
      variant: desktop
      compatibility_env: DESKTOP
```

映射键必须是已声明的 source 变体。

### `review`

| 字段 | 默认值 | 语义 |
| --- | --- | --- |
| `decisions_file` | 项目默认位置 | append-only 人工决策日志的基准路径；实际按月分片。 |
| `reviewer` | 当前 OS 用户 | Dashboard 修订记录中的操作者显示名，不是认证身份。 |

### `security`

`credential_rotation_completed_at` 和 `rotation_record` 是远端发布治理声明。只要 publish 中存在远端目标，两者都必须填写；本地发布不需要。不要为了通过校验而填写虚假值。

### `publish.targets[]`

共同字段：`type` 选择发布器，`prefix` 是目标内前缀，`versioned_prefix` 决定是否追加版本化路径，`timeout_seconds` 是远端超时。

| 类型 | 必需字段 | 对应可选依赖 |
| --- | --- | --- |
| `local` | `destination` | 无 |
| `github_release` | `repository`, `token_env`；`tag` 可选 | 无额外 Python 包 |
| `cloudflare_r2` | `bucket`, `access_key_env`, `secret_key_env`，以及 `account_id` 或 `endpoint_url` | `publish-r2` |
| `alibaba_oss` | `endpoint`, `bucket`, `sts_token_url`, `sts_token_env`；`sts_token_header` 可选 | `publish-oss` |

所有凭据字段保存的都只是环境变量名。

## 配置修改后的验证顺序

```powershell
localizer validate-config projects/my-project/project.yaml
localizer scan projects/my-project/project.yaml
localizer dashboard projects/my-project/project.yaml --host 127.0.0.1 --port 8765
```

先让结构校验通过，再检查扫描数量，最后看 Dashboard 预检。这样可以把 YAML 错误、资源匹配错误和运行时依赖错误分开定位。
