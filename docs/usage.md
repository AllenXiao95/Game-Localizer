# 通用使用指南

**中文** | [English](en/usage.md)

## 工作流概览

一个标准本地化周期包含：

```text
准备资源 → 校验配置 → 扫描 → 预览翻译 → QA/人工修订 → release 构建 → 发布
                                  ↘ SQLite TM 复用与沉淀 ↗
```

源资源保持只读；运行中间状态写入 `paths.workspace`，渲染结果和制品写入 `paths.output`，可复用译文写入 `tm.database`。

## 1. 准备资源与适配器

把原始资源放到 `paths.source`，然后按格式选择适配器：

| 资源格式 | `type` | 说明 |
| --- | --- | --- |
| PO/MO | `gettext` | 支持普通 gettext 和“键 + 源文”布局 |
| ParaTranz JSON | `paratranz_json` | 字段名可以在 `options` 中映射 |
| Paradox YAML | `paradox_yml` | 支持语言目录映射和格式校验 |

同一项目可以声明多个 adapter。每个 adapter 都有自己的 `include` 和 `exclude`，应避免让同一文件被多个 adapter 重复匹配。

## 2. 校验配置与扫描

```powershell
localizer validate-config projects/my-project/project.yaml
localizer scan projects/my-project/project.yaml
```

配置模型禁止未知字段。字段拼错会直接报错，而不会被静默忽略。扫描阶段不会请求模型，适合先验证路径和 glob。

## 3. 运行预览

```powershell
localizer build projects/my-project/project.yaml `
  --mode preview `
  --run-id preview-001
```

`run-id` 是本次运行的稳定标识，建议使用仅含字母、数字、点、横线或下划线的可读名称。不要复用已经完成的运行标识。

预览会产生：

- 每个资源的翻译结果；
- checkpoint 和 `run-state.json`；
- JSON/文本 QA 报告；
- 用于后续重建的运行证据。

预览允许保留待处理问题，不会生成可发布的 release 制品。

## 4. 使用 Dashboard 复核

```powershell
localizer dashboard projects/my-project/project.yaml --host 127.0.0.1 --port 8765
```

Dashboard 适合：

- 查看项目、资源、TM 和 tokenizer 预检状态；
- 启动本地任务并跟踪进度；
- 定位 QA 问题；
- 提交带操作者信息的人工修订；
- 基于父运行发起增量重建。

它是本机单用户工具，没有账户认证和多人审批。不要把写接口直接暴露到公网。

## 5. 从父运行增量重建

正式检查失败并完成人工修复后，可以复用父运行中源文仍一致的结果：

```powershell
localizer rebuild-from-run projects/my-project/project.yaml `
  --parent-run-id preview-001 `
  --run-id preview-002 `
  --mode preview
```

如果游戏版本也发生变化，可增加 `--version`。系统会核对源文指纹；源文已改变的记录不会被错误复用。

## 6. 构建正式制品

```powershell
localizer build projects/my-project/project.yaml `
  --mode release `
  --run-id release-001
```

`release` 与 `preview` 的关键区别是质量闸门：本次新增的 error 必须为零；配置了历史债务基线时，只允许已登记的存量问题继续存在。通过后会生成压缩包和 manifest。

manifest 是发布和回读验证的入口，包含内容哈希、版本、模式、QA 状态和构建元数据。不要仅凭 zip 文件名判断制品是否可发布。

## 7. 发布

按 `project.yaml` 中的目标发布：

```powershell
localizer publish projects/my-project/project.yaml path/to/artifact-manifest.json
```

只复制到本地目录：

```powershell
localizer publish-local path/to/artifact-manifest.json path/to/destination
```

远端发布需要相应可选依赖和环境变量凭据，但不会因为缺少轮换记录就推定凭据已经泄露。只有发生已知泄露、凭据误提交或强制轮换事件时，才设置 `security.credential_rotation_required: true`；此后远端发布保持 fail-closed，直到 `credential_rotation_completed_at` 和 `rotation_record` 都已填写。面板会在制品页、发布前展示当前状态，发布失败后也会显示每个目标的具体原因。

## 多资源变体

正式服、测试服或不同渠道可以属于同一个项目：

```yaml
paths:
  sources:
    stable: ./source/stable
    beta: ./source/beta
  default_variant: stable
  workspace: ../../var/my-project/workspace
  output: ../../var/my-project/output
```

运行指定变体：

```powershell
localizer build projects/my-project/project.yaml `
  --variant beta `
  --mode preview `
  --run-id beta-001
```

变体共享 TM、术语表和规则；workspace/output 会自动增加变体子目录。TM 命中仍核对源文指纹，因此相同坐标但源文不同的条目不会误用。

## 运行数据的保留策略

建议纳入版本控制：

- `project.yaml`
- `prompt.md` 和 `background.md`
- `glossary.yaml` 和 `rules.yaml`

建议排除版本控制并定期备份：

- `.env`
- SQLite TM 及其 WAL/SHM 文件
- `workspace` 与 `output`
- tokenizer 缓存

SQLite TM、人工决策日志和已发布 manifest 不是普通临时文件。迁移、清理或升级前应生成一致性备份。
