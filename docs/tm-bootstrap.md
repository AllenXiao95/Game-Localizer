# 从存量汉化资源构建 TM

**中文** | [English](en/tm-bootstrap.md)

## 为什么不能直接“猜”未知结构

任意存量文件里，框架无法可靠推断哪个字段是稳定键、哪个是源文、哪个是译文，也无法知道文件移动后坐标是否仍应相同。错误推断会比漏导更危险：译文可能挂到另一个资源键上，并在后续版本被自动复用。

因此接入分两类：

1. 现有结构已被 adapter 支持：直接从资源预检并构建 TM。
2. 结构未知或暂不支持：先显式映射成中立的 TM Seed JSON，再导入。

两条流程都默认 dry-run。只有提供 `--apply --accepted-by <操作者>` 才会把通过校验的存量译文写成 `reviewed + formal + human` 记录。

## 路径一：直接从受支持资源构建

### 支持情况

| 结构 | 可直接读取已有译文的条件 |
| --- | --- |
| Gettext PO/MO | `layout: standard`；`msgid` 是源文，`msgstr` 是已有译文 |
| ParaTranz JSON | 每项同时具有配置映射的 key/source/translation 字段 |
| Paradox YAML | source locale 文件与 target locale 同名资源按 adapter 目录规则并列存在 |

`gettext` 的 `keyed_source` 布局把 `msgstr` 当源文，本身不包含目标译文，不能单独用于这条 bootstrap 流程。

### 1. 配置资源

先让 `paths.source` 指向存量资源根目录，并配置正确 adapter。以标准 PO 为例：

```yaml
paths:
  source: ./existing-localization

resources:
  adapters:
    - type: gettext
      include: ["**/*.po"]
      options:
        layout: standard
        empty_source: skip
        source_filter: all
```

### 2. 扫描与 dry-run

```powershell
localizer validate-config projects/my-project/project.yaml
localizer scan projects/my-project/project.yaml
localizer tm-bootstrap-resources projects/my-project/project.yaml
```

最后一条命令不会写 TM，会把报告写到：

```text
<workspace>/reports/tm-bootstrap-resources.json
```

报告中的关键字段：

- `total`：adapter 抽取出的全部条目；
- `accepted`：具有非空译文且通过占位符、语言规则和术语检查的条目；
- `rejected`：至少存在一个阻断问题的坐标数；
- `issue_counts` / `issues`：问题分类和具体坐标。

只要 `rejected` 不为零，正式写入就会整体拒绝，不会只导入一部分后留下难以解释的半成品。

### 3. 人工确认后写入

```powershell
localizer tm-bootstrap-resources projects/my-project/project.yaml `
  --apply `
  --accepted-by localization-owner
```

已有 SQLite 会先通过 SQLite backup API 备份到数据库同级 `backups` 目录；全新项目会直接创建数据库和表。写入报告记录操作者、备份路径和最终数量。

如果项目使用多资源变体，需要显式选择：

```powershell
localizer tm-bootstrap-resources projects/my-project/project.yaml `
  --variant stable `
  --apply `
  --accepted-by localization-owner
```

## 路径二：未知结构转换为 TM Seed

### Seed 的坐标语义

每条记录必须能映射为以下五个字段：

| 字段 | 含义 | 必须如何确定 |
| --- | --- | --- |
| `adapter_id` | 后续读取正式资源的 adapter | 必须与 `project.yaml` 中配置的 `type` 相同 |
| `relative_path` | 资源相对于 `paths.source` 的路径 | 必须与 adapter 扫描结果一致，使用 `/` |
| `logical_key` | 文件内稳定键 | 必须与 adapter 提取出的 key 一致 |
| `source_text` | 原始源语言文本 | 用于生成源文指纹 |
| `translation` | 已有目标语言译文 | 不能为空，并会经过 QA |

`project_id` 和源/目标 locale 不允许由 Seed 覆盖，而是从 `project.yaml` 获取，避免把另一项目的数据误写进当前 TM。

如果原始结构没有稳定键，可以在转换时生成确定性的键，例如 JSON 数组中的业务 ID。不要使用会随排序变化的临时序号，除非正式 adapter 之后也使用完全相同的序号。

### 单文件示例

适合一个存量资源文件，或同一 adapter/path 下的一组条目：

```json
{
  "schema_version": 1,
  "defaults": {
    "adapter_id": "paratranz_json",
    "relative_path": "ui/menu.json"
  },
  "entries": [
    {
      "logical_key": "menu.start",
      "source_text": "Start Game",
      "translation": "开始游戏",
      "context": "main menu button"
    },
    {
      "logical_key": "menu.exit",
      "source_text": "Exit",
      "translation": "退出"
    }
  ]
}
```

可直接复制的文件见 [`examples/tm-seed-single.json`](examples/tm-seed-single.json)。

### 一个 Seed 汇总多个资源文件

如果每条记录来自不同路径，可以不写 defaults，在条目内提供完整坐标：

```json
{
  "schema_version": 1,
  "entries": [
    {
      "adapter_id": "gettext",
      "relative_path": "ui/menu.po",
      "logical_key": "Start Game",
      "source_text": "Start Game",
      "translation": "开始游戏"
    },
    {
      "adapter_id": "gettext",
      "relative_path": "items/names.po",
      "logical_key": "Mana Potion",
      "source_text": "Mana Potion",
      "translation": "法力药水"
    }
  ]
}
```

### 多个 Seed 文件批量导入

大项目建议每个原始资源生成一个 Seed，以便审阅和增量重跑：

```text
tm-seed/
├── ui-menu.json
└── item-names.json
```

两个文件都使用单文件格式，并通过一条命令传入：

```powershell
localizer tm-import-seed projects/my-project/project.yaml `
  docs/examples/tm-seed-multi/ui-menu.json `
  docs/examples/tm-seed-multi/item-names.json
```

可复制示例见 [`examples/tm-seed-multi`](https://github.com/AllenXiao95/Game-Localizer/tree/master/docs/examples/tm-seed-multi)。所有输入会先合并校验；跨文件出现相同 `adapter_id + relative_path + logical_key` 时会报 `duplicate_coordinate`。

### dry-run 与正式导入

先预检：

```powershell
localizer tm-import-seed projects/my-project/project.yaml path/to/seed.json
```

报告默认写到：

```text
<workspace>/reports/tm-seed-import.json
```

确认报告后写入：

```powershell
localizer tm-import-seed projects/my-project/project.yaml path/to/seed.json `
  --apply `
  --accepted-by localization-owner
```

也可以用 `--report` 和 `--backup` 指定报告及备份位置。目标文件已存在时备份路径必须是一个尚不存在的文件，防止覆盖旧备份。

## 未知结构的转换步骤

对任意 JSON、CSV、XML 或自定义文本，先回答下面五个问题：

1. 一条翻译记录的边界是什么？
2. 哪个字段或组合能长期唯一标识它？
3. 源文和译文分别在哪里？
4. 它最终会被哪个 adapter 读取？
5. adapter 将报告什么 `relative_path` 和 `logical_key`？

然后编写一次性只读转换器，把答案映射为 TM Seed。转换器不应修改原文件；应统计输入条目数、输出条目数和跳过原因。先用少量样本运行 `tm-import-seed`，再处理全量。

如果第 4、5 个问题无法回答，说明缺的不只是 TM 导入格式，还缺少正式资源 adapter。此时应先实现 adapter；否则导入的 TM 坐标不会与未来构建产生的坐标一致，数据库虽然有数据却永远无法命中。

## 写入后的验证

```powershell
localizer build projects/my-project/project.yaml --mode preview --run-id tm-check
```

检查输出中的：

- `tm_hits` 是否接近导入的有效坐标数；
- `machine_successes` 是否只覆盖真正新增或源文变化的条目；
- QA 报告是否没有因坐标错配出现异常译文；
- 随机抽样的 `relative_path + logical_key + source_text` 是否与原资源一致。

不要只检查 SQLite 行数。TM 的价值取决于坐标和源文指纹能否与正式 adapter 的提取结果一致。
