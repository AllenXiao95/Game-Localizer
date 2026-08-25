# Game Localizer 核心架构与 Adapter 契约

[中文] | [English](en/core-architecture.md)

本文描述 Game Localizer 的稳定核心边界。M8、Dashboard、CLI、Tauri 或未来的智能辅助能力都必须建立在这些边界之上，而不能重新定义资源、翻译记忆、QA、构建或发布语义。

## 1. 核心模型

Game Localizer 不把完整资源文件转换成 TM，也不从 TM 重新生成完整资源文件。标准流程是：

```text
Original resource
      │
      ▼
ResourceAdapter
  projection
      │
      ▼
TranslationUnit[]
      │
      ├── TM resolution
      ├── model-assisted translation
      ├── Prompt / glossary / rules
      ├── deterministic QA
      └── human review
      │
      ▼
resolved translations
      │
      ▼
ResourceAdapter.render(original source, resolved units)
      │
      ▼
localized resource
```

四个核心约束：

1. **原始资源是结构 source of truth。** 文件结构、顺序、不可翻译内容和格式专有信息不由 TM 接管。
2. **ResourceAdapter 负责格式特定的投影与重建。** Adapter 把资源投影成 translation core 可以消费的工作单元，并基于原始资源结构回写译文。
3. **TranslationUnit 是 translation core 的标准工作单元边界。** 它不是所有本地化资源的万能序列化 IR；复杂格式可以由 Adapter 投影成 0/1/N 个 TranslationUnit，并自行保留重建所需的格式信息。
4. **翻译知识、provenance、review/authority state 是领域状态，由 TM repository 持久化。** SQLite TM 不是资源结构权威，也不是资源 interchange format。

## 2. ResourceAdapter 的责任边界

Adapter 负责：

- 识别自己能够处理的资源；
- 扫描并提供稳定资源身份；
- 从格式专有结构投影出 TranslationUnit；
- 保持逻辑坐标稳定，避免 silent identity collision；
- 在需要时暴露 translation-level context、placeholder constraints 或其他语义信息；
- 基于原始 source 与已解析译文重建目标资源；
- 校验重建后的资源是否满足该格式的基本有效性；
- 在格式本身编码目标语言路径时规划 destination。

Adapter 不自动拥有所有“与格式有关”的工作。例如归档、签名、安装、上传、调用外部编译器等可以属于 Build / Artifact / Publish application service，而不是强行塞进 Adapter。

## 3. TranslationUnit 的边界

TranslationUnit 应只包含 translation core 真正需要消费的语义，例如：

- 稳定逻辑身份；
- source text；
- 已有/候选 translation；
- source / target locale；
- translation context；
- placeholder 或其他约束；
- 需要进入 QA、Prompt、Review 的少量 metadata。

以下信息不应仅因为某个新格式需要就自动升级为 TranslationUnit 一级字段：

- XML/AST 节点类型；
- quote style / indentation / line number；
- 格式专有 header；
- 编译器参数；
- 任意 selector / variant / node graph。

判断原则：**如果 TM、Prompt、QA、Review 不需要理解该信息，就优先留在 Adapter 或 opaque metadata 中。**

## 4. TM repository 的边界

TM 持久化翻译知识与治理状态，例如：

- stable coordinate 与 source fingerprint；
- translation；
- provenance / run / model / Prompt revision；
- review / quality / formal state；
- match scope 与历史迁移信息。

TM 不负责：

- 保存完整源文件结构；
- 从数据库反向生成任意资源；
- 解析 PO/YAML/JSON 等源格式；
- 直接修改游戏资源文件；
- 替代 Adapter 的 reconstruction contract。

资源更新后的正常路径是：重新由 Adapter 投影当前资源，比较 stable coordinate 与 source fingerprint，通过 TM 决定复用、stale 或重译，再走正常 render/build。

## 5. Application services 的权威

ProjectRunner、TranslationPlanner、QA / QualityGate、Review、Build、Artifact、Publish 等 Python application services 负责确定性工作流和治理边界。

CLI、Dashboard、Tauri 和 Controlled Intelligence 都是这些服务的调用者。它们不得各自实现一套新的：

- 资源解析/回写规则；
- TM authority policy；
- QA 判据；
- Build / Release / Publish 语义。

未来的智能工具应暴露 application-level capability，例如：

```text
project.inspect
translation.plan
tm.audit
prompt.evaluate
build.preview
release.readiness
```

而不是把低层能力泛化成 Agent 逃生口：

```text
po.write
yaml.patch
sqlite.execute
filesystem.write
arbitrary shell
```

## 6. Adapter 行为契约

当前 Python `ResourceAdapter` Protocol 定义接口形状；本节定义更高层行为不变量。

### 6.1 Identity

- 同一个逻辑词条仅修改源文时，逻辑坐标应尽量保持稳定；源文变化通过 source fingerprint 表达。
- 同一资源内出现会导致 stable identity 冲突的重复坐标时，必须 deterministic fail 或使用格式明确支持的 disambiguation；不得静默覆盖。

### 6.2 Projection

- 一个资源节点可以投影为 0、1 或多个 TranslationUnit。
- Adapter 不应要求 translation core 理解源格式 AST 才能工作。
- 格式专有结构不应仅为“统一”而丢失。

### 6.3 Reconstruction

- `render` 必须以原始 source 为结构基础，而不是仅根据 TM rows 重建整个资源。
- 未修改或未进入翻译范围的内容必须保持语义不变。
- 只改变一个 TranslationUnit 时，不应无理由改变其它 translation payload 或不可翻译结构。

### 6.4 Round-trip

所有正式支持的 Adapter 至少应满足 **semantic round-trip**：

```text
extract(source)
→ render(unmodified units, source)
→ extract(rendered)
```

投影出的逻辑身份和可翻译语义应等价。

`byte-preserving round-trip` 是更强能力，可以按格式声明，不作为所有 Adapter 的通用硬要求。

### 6.5 Partial update

Adapter 应有回归样本证明：只修改一个 work unit 后，重建资源中只有预期 translation payload 发生语义变化，其余结构保持稳定。

## 7. Adapter Conformance 的方向

在继续扩大格式支持之前，优先建立现有 Adapter 的 conformance coverage。第一批应覆盖当前公开支持的：

- Gettext PO/MO；
- ParaTranz JSON；
- Paradox YAML。

建议统一验证：

```text
fixture
→ probe / scan / extract
→ identity assertions
→ no-op render
→ semantic round-trip
→ single-unit mutation
→ re-render
→ preservation assertions
```

外部游戏/项目首先作为 compatibility corpus 和 falsification target：它们用于发现当前公开承诺中的真实缺陷，或证明多个项目反复需要同一个新抽象；不会因为某个项目使用一种额外格式或私有语法，就自动扩大 Game Localizer 的支持边界。

## 8. 对 M8 的约束

M8 是现有 localization core 的消费者，而不是第二套 localization architecture。

Controlled Intelligence 可以：

- 汇总证据；
- 生成 reviewable proposal；
- 调用白名单 application services；
- 在明确权限边界内请求 Apply / Release / Publish。

但不能：

- 绕过 Adapter 直接理解并修改任意资源格式；
- 把 TM 当成资源 IR；
- 建立第二套 QA / Build / Publish 语义；
- 让模型输出成为 authority policy。

即使未来完全移除 Controlled Intelligence，Game Localizer 的确定性本地化流水线仍应完整可用。
