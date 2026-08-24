# M8 EvalCase v1 契约

**中文** | [English](en/m8-evalcase-v1-contract.md)

- 状态：首个公开评测切片的 maintainer contract
- Parent roadmap：#30
- Parent capability：#9
- 协调任务：#13 fixtures/checks、#15 schema validation
- 契约版本：`EvalCase schema_version = 1`

## 1. 为什么需要这个契约

#13 被刻意设计为 contributor-sized 任务，#15 会独立实现 schema 校验。两者不能各自发明一套评测格式。

本文只冻结二者共同需要的**最小契约**。它不是 evaluator 实现，也不依赖 AgentRuntime、模型 Provider、baseline runner 或 semantic judge。

边界如下：

```text
EvalCase 定义
    +
Evaluation candidate
    ↓
与 Provider 无关的 deterministic check
    ↓
AssertionResult 证据
```

Case 定义中不得包含 Provider 凭据、隐藏 Prompt 材料、运行时秘密，也不得包含禁止进入 Prompt composition 的 locked-test 答案。

## 2. 目录边界

目标目录：

```text
evals/i18n-prompt/
├── manifest.yaml
├── cases/
│   ├── dev/
│   │   ├── protocol.jsonl
│   │   └── placeholders.jsonl
│   └── locked/
├── rubrics/
└── baselines/
```

对 #13 而言，只要求 `cases/dev/protocol.jsonl` 与 `cases/dev/placeholders.jsonl`。

`locked/` 的含义是：**不得进入 Prompt/background/few-shot composition path**。它不意味着提交在公开仓库中的文件是秘密数据。Prompt 构造代码不得读取 locked cases。

`rubrics/` 和 `baselines/` 属于后续 #9 范围，不属于 #13/#15 的首轮实现。

## 3. EvalCase v1

公开 case 使用 JSONL，每行一个 JSON object。

### 3.1 必填字段

| 字段 | 类型 | 契约 |
| --- | --- | --- |
| `schema_version` | integer | 本契约必须为 `1`。 |
| `id` | string | 稳定的小写 case ID，见 §3.3。 |
| `source_locale` | string | 规范化的 BCP-47 风格 locale，如 `en-US`。 |
| `target_locale` | string | 规范化的 BCP-47 风格 locale，如 `zh-Hans`。 |
| `domain` | string | 简短稳定领域，如 `game-ui`；不要编码真实私有项目名。 |
| `source` | string | 提交给翻译系统的源文。 |
| `assertions` | array | 一个或多个 §4 中定义的 assertion object。 |
| `provenance` | object | §3.4 定义的来源/再分发元数据。 |

### 3.2 可选字段

| 字段 | 类型 | 契约 |
| --- | --- | --- |
| `context` | object | 评测所需的非敏感上下文，例如页面角色、长度预算。 |
| `required_tokens` | array[string] | assertion 使用时必须保留/出现的 literal token，默认 `[]`。 |
| `references` | array[string] | 适合 reference scoring 时的可接受参考译文，默认 `[]`；#13 不需要评分。 |
| `difficulty` | string | `basic`、`intermediate` 或 `advanced`。 |
| `tags` | array[string] | 稳定的小写检索/过滤标签。 |

未来新增字段不得静默改变 v1 既有字段的含义。#15 可以选择拒绝未知字段，或显式定义向前兼容扩展点，但不得重解释已有字段。

### 3.3 稳定 ID

使用小写 ASCII segment，并用 `.` 分隔。推荐：

```text
<dimension>.<subtype>.<three-digit-sequence>
```

例如：

```text
placeholder.printf.001
placeholder.python.004
protocol.item-count.002
protocol.terminator.003
```

Case ID 合并后视为稳定身份，不得复用于语义已经变化的 case。若 case 含义发生实质变化，应新增 ID。

### 3.4 Provenance

`provenance` 至少包含：

```json
{
  "kind": "synthetic",
  "license": "GPL-3.0-or-later"
}
```

v1 允许的 `kind`：

- `synthetic`
- `public-domain`
- `compatible-licensed`
- `authorized-regression`

`license` 必须是明确的 SPDX 风格 license expression，或 maintainer 接受的其他清晰再分发声明。不得填写 `unknown`、`fair-use` 或空值。

外部/派生材料还应记录 `source` 标识或 URL；如有转换，记录简短 `transformation`。`authorized-regression` 必须脱敏，不得暴露受限制的游戏/项目文本。

## 4. Assertion vocabulary v1

Assertion 使用 object，而不是裸字符串：

```json
{
  "name": "placeholder_integrity",
  "severity": "hard",
  "params": {
    "syntax": "printf",
    "allow_reorder": true
  }
}
```

`severity` 为 `hard` 或 `report`。#13 的 protocol/placeholder deterministic regression 使用 `hard`。

### 4.1 #13 必须实现的 assertion

#### `protocol_complete`

独立于翻译质量，检查返回协议结构。参数可包括：

- `expected_items`：正整数；
- `terminator`：协议使用终止符时的可选 literal；
- `allow_extra_items`：boolean，默认 `false`。

通过条件是协议结构完整。缺少 item、不允许的额外 item、item boundary malformed、必需 terminator 缺失或 malformed 都应失败。

#### `placeholder_integrity`

检查**placeholder identity + multiplicity**，不是普通 set membership。刻意不用 `placeholder_set` 命名，因为 `%s %s` 变成 `%s` 必须失败。

参数：

- `syntax`：`auto`、`printf`、`python`、`icu` 或 `custom`；
- `allow_reorder`：boolean，默认 `true`。

通过条件是每个预期 placeholder/token 的出现次数和内容都得到保留，没有 mutation、丢失或非预期 duplication。只有在 `allow_reorder=true` 且对应语法允许时才能重排。

### 4.2 为 #9/#15 预留的 deterministic 名称

以下 assertion name 属于合法 v1 vocabulary，但 **#13 不要求实现**：

- `number_fidelity`
- `required_tokens`
- `forbidden_renderings`
- `max_chars`

其可执行语义由 #9 实现。#15 可以校验结构，但首个 #13 PR 应聚焦 protocol 与 placeholder。

新增 assertion name 属于 additive contract change，必须先文档化后才能被 fixture 使用；已有 assertion name 不得静默改义。

## 5. Case definition 不等于 candidate output

公开 case file 描述的是**评测什么**，不是某次模型/Provider 输出的 canonical answer。

Deterministic checker 接收独立 candidate，概念结构如下：

```json
{
  "case_id": "placeholder.printf.001",
  "text": "Welcome, %s! You have %d coins.",
  "raw_response": null
}
```

- `text`：用于 content assertion 的已解码 candidate translation；
- `raw_response`：可选。当 `protocol_complete` 需要检查 Provider 原始 batch/protocol response 时使用。

#13 可以在测试代码中使用少量 candidate test vector。它们是测试输入，不替代公开 EvalCase schema。

## 6. Deterministic result 契约

每个 check 都必须输出结构化 evidence。最少包含：

```json
{
  "case_id": "placeholder.printf.001",
  "assertion": "placeholder_integrity",
  "passed": false,
  "severity": "hard",
  "code": "placeholder_missing",
  "message": "Expected placeholder %d is missing.",
  "evidence": {
    "expected": ["%s", "%d"],
    "actual": ["%s"]
  }
}
```

必填 result field：

- `case_id`
- `assertion`
- `passed`
- `severity`
- `code`
- `message`
- `evidence`（object；通过时可以为空）

`code` 必须 machine-stable；人类可读 `message` 可以改进而不改变 result identity。Evidence 只应包含理解/复现 deterministic result 所需的最小事实，不得包含凭据或无关原文。

建议 #13 failure code：

```text
protocol_missing_item
protocol_extra_item
protocol_malformed_item
protocol_missing_terminator
placeholder_missing
placeholder_extra
placeholder_mutated
placeholder_count_mismatch
```

这些 code 对 #13 是 guidance；如果实现过程中发现更清晰的最小划分，可以在 merge 前由 maintainer 调整，但 result field 结构应保持稳定。

## 7. 最小示例

该示例仅用于说明 contract，不属于 #13 fixture corpus：

```json
{
  "schema_version": 1,
  "id": "placeholder.printf.001",
  "source_locale": "en-US",
  "target_locale": "zh-Hans",
  "domain": "game-ui",
  "source": "Welcome, %s! You have %d coins.",
  "required_tokens": ["%s", "%d"],
  "references": [],
  "assertions": [
    {
      "name": "placeholder_integrity",
      "severity": "hard",
      "params": {"syntax": "printf", "allow_reorder": true}
    }
  ],
  "provenance": {"kind": "synthetic", "license": "GPL-3.0-or-later"},
  "difficulty": "basic",
  "tags": ["printf", "placeholder"]
}
```

## 8. 各 Issue 的兼容责任

### #13

实现 synthetic dev fixtures，以及 `protocol_complete` / `placeholder_integrity` 的 provider-independent deterministic checks。不得另建 schema。

### #15

把同一个 v1 field meaning 和 assertion vocabulary 编码为 actionable offline schema validation。不得另建 case model。

### #9

负责后续 evaluator/report/baseline runtime 和 additive assertion vocabulary。未来若出现 major schema version，必须继续接收有效 v1 case 或提供明确迁移路径。

## 9. 本前置任务不覆盖的内容

本契约不定义 semantic model judge、BLEU/COMET 类指标、Prompt patch、baseline acceptance policy、Provider adapter、AgentRun integration、release gate 或隐藏 benchmark 分发机制。
