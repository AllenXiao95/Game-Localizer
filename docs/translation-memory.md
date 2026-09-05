# TM 与 SQLite 指南

**中文** | [English](en/translation-memory.md)

## 基本概念

TM（Translation Memory，翻译记忆）保存已经产生的译文，供后续版本复用。当前项目使用 SQLite，而不是一个简单的“源文 → 译文”字典，因为还需要记录：

- 项目、适配器、资源相对路径和逻辑键组成的稳定坐标；
- 源文指纹，防止源文变化后误用旧译文；
- 机器、人工或旧系统等来源；
- 审核、质量、正式状态和运行标识；
- 旧 TM 同步记录与权威源状态。

`project.yaml` 只需要声明数据库路径：

```yaml
tm:
  database: ../../var/my-project/localizer.sqlite
  global_exact_match: reviewed_only
  commit_policy: quality_gate
```

相对路径以 `project.yaml` 所在目录为基准。

## 新项目如何创建 SQLite TM

无需安装 SQLite 命令行工具，也不要手工创建表。下面任一写流程都会自动创建父目录、数据库文件、schema 和索引：

- `localizer build ...`；
- `localizer tm-sync-legacy ...`；
- Dashboard 中实际执行的构建或人工写入操作。

推荐的首次创建流程：

```powershell
localizer validate-config projects/my-project/project.yaml
localizer scan projects/my-project/project.yaml
localizer build projects/my-project/project.yaml --mode preview --run-id tm-init
```

前两个命令是只读检查；第三个命令打开写连接并创建 SQLite。即使预览没有产生可提交的正式译文，数据库结构也会存在。

确认文件已经创建：

```powershell
Test-Path var\my-project\localizer.sqlite
```

如果配置使用了别的路径，以 `tm.database` 解析后的实际位置为准。Dashboard 的项目概览也会显示 TM 路径、schema 和统计信息。

## “建立 TM”与“写入正式译文”的区别

创建 `.sqlite` 文件只代表数据库可用，不代表里面已经有可复用译文。

- `preview` 用于生成候选、报告和 checkpoint；数据库可以被初始化，但机器候选不会因为一次预览就自动成为正式真相。
- `release` 通过 QualityGate 后，符合策略的机器结果才会写入正式 TM。
- Dashboard 的人工定稿会写入带审计信息的正式记录，且优先级高于机器结果。
- 旧 JSON 导入的记录先以 legacy 分类进入 SQLite，是否可复用取决于分类、审核状态和 `global_exact_match` 策略。

### Release 基线连续性

一次成功的 `release` 不只是生成正式制品，也会完成本轮新增机器译文的 authority transition。对本次 release 实际采用、且符合提交策略的机器译文：

- 当前运行新调用 Provider 生成的机器译文，在完整 QualityGate 通过后可以晋升为正式 TM；
- `rebuild-from-run --mode release` 安全复用的父 checkpoint 机器译文，如果实际进入本次 release，也必须在同一个 QualityGate 通过后进入本次正式 TM 基线；
- 已经是正式/人工审核 TM 命中的记录保持原有 authority，不需要因为被 release 使用而重新晋升；
- `preview` 和 `preview rebuild` 仍然只产生候选与运行证据，不会因为复用了 checkpoint 就获得正式 authority。

因此，对相同 source/config，在一次成功 release 后立即重新规划时，本次 release 新获得 authority 的 eligible machine translations 不应再次作为 Provider `pending` 工作出现。若制品已经正式可发布，而同一批译文在 TM 中仍是 non-formal 并导致下一轮重译，应视为 release artifact 与 TM baseline 的一致性缺陷，而不是正常缓存未命中。

## 旧 JSON TM 转 SQLite

如果手里没有旧 TM JSON、只有已经汉化的资源文件，请使用[从存量汉化资源构建 TM](tm-bootstrap.md)中的资源 bootstrap 或中立 TM Seed 流程。本节命令只针对旧脚本的兼容 TM 格式。

### 适用范围

`tm-sync-legacy` 用于迁移旧脚本生成的 `history_tm.json` 兼容格式。它不是任意 JSON 到 SQLite 的通用导入器。兼容文件的逻辑结构为：

```json
{
  "relative/path/to/catalog": {
    "logical_key": {
      "ru": "source text",
      "zh": "translated text"
    }
  }
}
```

其中外层键会作为资源相对路径，第二层键会作为逻辑键。`ru`/`zh` 是旧格式保留下来的固定字段名；实际写入 SQLite 的语言标识仍取当前项目的 `languages.source` 与 `languages.target`。如果现有 TM 不是这个结构，需要先写一个显式转换器或转换成该兼容结构，不能只修改扩展名。

### 1. 迁移前备份

至少保留：

- 原始旧 TM；
- 当前 `project.yaml`、术语表和规则；
- 已存在的 SQLite TM（如果有）；
- 一份能够代表当前发布结果的制品或行为基线。

不要覆盖原始 JSON。导入器本身按只读方式读取它，并会额外生成写保护标记文件。

### 2. 校验项目配置

```powershell
localizer validate-config projects/my-project/project.yaml
```

旧记录会使用当前配置中的项目 ID、语言、术语和 QA 规则分类。因此导入前要先把这些配置定下来；之后修改 `project.id` 会改变稳定坐标。

### 3. 执行同步

```powershell
localizer tm-sync-legacy `
  projects/my-project/project.yaml `
  path/to/history_tm.json
```

命令会：

1. 创建或打开 `tm.database` 指定的 SQLite；
2. 解析旧 JSON 并生成稳定坐标和源文指纹；
3. 用占位符、语言规则和术语表对历史译文分类；
4. 写入 legacy 影子记录；
5. 在 `paths.workspace/reports/legacy-tm-migration.json` 写迁移报告；
6. 在旧 JSON 旁创建 `.shadow-sync.lock` 写保护标记。

同一文件内容未变化时再次运行会按哈希跳过。只有在明确需要重做分类或重新同步时才使用：

```powershell
localizer tm-sync-legacy projects/my-project/project.yaml path/to/history_tm.json --force
```

同步会替换该项目已有的 legacy 影子行，但不能覆盖 SQLite 中受保护的人工或正式记录。

### 4. 阅读迁移报告

重点检查：

- `total` 与预期旧记录数是否一致；
- `imported` 是否与本轮计划一致；
- `classifications` 中 clean、suspect、quarantined 的分布；
- `reasons` 中空译文、未翻译、占位符不一致、规则或术语违规的数量；
- `skipped_unchanged` 是否符合预期。

quarantined 记录不会被当作正常译文静默复用，而会留给人工处理或重新翻译。

## 从已验收制品建立 TM 基线

如果历史系统没有可信 TM，但有一个已经人工验收的正式制品，可以先 dry-run 分析：

```powershell
localizer tm-adopt-artifact `
  projects/my-project/project.yaml `
  path/to/artifact-manifest.json
```

确认映射和报告后再写入：

```powershell
localizer tm-adopt-artifact `
  projects/my-project/project.yaml `
  path/to/artifact-manifest.json `
  --apply `
  --accepted-by project-owner
```

应用时会生成 SQLite 备份和数据基线报告。`--accepted-by` 应填写真实、可追溯的操作者标识。

然后验证从该基线重新构建的行为：

```powershell
localizer tm-verify-artifact `
  projects/my-project/project.yaml `
  path/to/artifact-manifest.json `
  --run-id verify-001
```

## 何时切换 SQLite 为权威源

对全新项目，不需要执行旧系统迁移治理命令。对仍有旧程序写 `history_tm.json` 的迁移项目，`tm-sync-legacy` 只建立影子库；确认旧入口已冻结、数据和行为已核验后，才考虑 `tm-switch-authority`。

```powershell
localizer tm-switch-authority projects/my-project/project.yaml `
  --behavior-baseline path/to/behavior-baseline.json `
  --data-baseline path/to/data-baseline.json `
  --legacy-tm path/to/history_tm.json
```

切换前置条件包括：

- SQLite 尚未标记为权威源；
- 已完成至少一次 legacy 同步；
- 最终同步记录的哈希仍与旧 JSON 当前内容一致；
- legacy 行数与同步证据一致；
- 行为基线和数据基线是有效、独立的证据文件。

这是单向治理动作，不是每次构建都要执行的初始化命令。重复切换会被拒绝。

## SQLite 导回旧 JSON

需要回滚到旧入口时，使用显式导出，不要直接查询表后拼 JSON：

```powershell
localizer tm-export-legacy `
  projects/my-project/project.yaml `
  path/to/exported-history-tm.json
```

默认行为：

- 不覆盖已存在文件；
- 不导出 quarantined/unknown 行；
- 为旧格式无法表达的人工、正式和审核属性生成 provenance 边车。

需要覆盖或逐行往返时分别使用 `--overwrite`、`--include-quarantined`。后者可能把旧格式无法标记的问题记录暴露给旧程序，应先阅读导出报告。

## 备份与并发注意事项

- 不要在 Dashboard 任务运行时用外部工具修改数据库。
- 同一个 TM 由任务队列串行写入；多资源变体共享 TM 时同样如此。
- 复制数据库时应使用应用提供的备份流程或 SQLite backup API，避免只复制主文件而遗漏 WAL 中的数据。
- 不要把 `.sqlite-wal`、`.sqlite-shm` 当作可独立恢复的备份。
- 人工决策日志是审计来源，TM 是其可用投影；两者都应纳入备份策略。

## 常见问题

### 为什么同步后 TM 命中数仍然很少

常见原因是资源相对路径或逻辑键与旧记录不一致、源文指纹发生变化，或者历史译文被分类为 quarantined。先看迁移报告，再对照扫描结果，不要通过放宽指纹核对来强行提高命中率。

### 可以用 SQLite Browser 手工改译文吗

不建议。直接修改会绕过人工决策日志、保护规则和审计字段。日常修订应通过 Dashboard；批量迁移应使用迁移命令或新增经过测试的专用导入器。

### 删除 SQLite 能重新开始吗

技术上可以，但会丢失翻译、审核状态和迁移证据。仅在确认数据库没有需要保留的数据且已有备份时才这样做。通常更安全的是使用新的 `tm.database` 路径做试迁移，验证完成后再决定切换。
