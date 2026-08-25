# M8：质量工程、受控智能与客户端产品化

**中文** | [English](en/milestone-m8-agent-client.md)

- 状态：设计提案
- 适用范围：本地单人游戏本地化流水线
- Roadmap umbrella：[#30](https://github.com/AllenXiao95/Game-Localizer/issues/30)
- 核心架构约束：[Game Localizer 核心架构与 Adapter 契约](core-architecture.md)

## 1. M8 的定位

M8 **不是**把 Game Localizer 改造成通用 Agent 平台，也不是第二套 localization architecture。

现有确定性内核已经负责：

```text
ResourceAdapter
→ TranslationUnit
→ TM resolution / translation / QA / review
→ Build
→ ResourceAdapter.render(original source, resolved units)
→ Release / Publish
```

M8 只能建立在这套内核之上。即使删除全部 Controlled Intelligence，CLI / Dashboard 的确定性翻译、审查、构建和发布流程仍必须完整可用。

M8 现在拆成三个相互关联但不要求整体交付的 track：

```text
Track A — Quality Engineering
├── Eval Harness
└── deterministic TM Audit

Track B — Controlled Intelligence
├── Prompt Workbench
├── optional TM Repair Advisor
└── optional Release Readiness Advisor

Track C — Productization
└── Windows Tauri client
```

这三个 track 不是一条“AgentRuntime → TM Agent → Prompt Agent → Build Agent → Desktop”的瀑布路线。

## 2. 不可破坏的核心边界

M8 必须遵守项目级架构 invariant：

1. 原始 resource 是结构 source of truth。
2. ResourceAdapter 负责格式特定 projection / reconstruction。
3. TranslationUnit 是 translation core 消费的标准工作单元，而不是万能资源 IR。
4. 翻译知识、provenance、review/authority state 是领域状态，由 TM repository 持久化；TM 不是资源 interchange format。
5. ProjectRunner、Planner、QA / QualityGate、Review、Build、Artifact、Publish application services 保持确定性工作流权威。
6. CLI、Dashboard、Tauri 与 Controlled Intelligence 都是 application services 的调用者，不能拥有平行的资源、TM、QA、Build 或 Publish 语义。

Agent/Advisor tool 应保持 application-level 粒度，例如：

```text
project.inspect
translation.plan
tm.audit
prompt.evaluate
build.preview
release.readiness
```

不把下列低层能力暴露为模型逃生口：

```text
arbitrary shell
raw SQL
raw Python
unscoped filesystem writes
po.write / yaml.patch / adapter.raw_render
```

## 3. Track A — Quality Engineering

Quality Engineering 是 M8 中最确定、即使未来不做 Agent 也有独立价值的部分。

### 3.1 Eval Harness — #9

目标：可重复回答“Prompt / model / config 的改动是否提高本地化质量，同时没有引入确定性回归，以及成本/延迟发生了什么变化”。

实施原则：

- deterministic assertions 是第一层权威；
- baseline diff 是核心产物；
- model judge 仅用于无法确定性表达的语义/风格维度，首版 report-only；
- public / development / locked cases 必须保持清晰边界；
- Eval 不重新实现 resource parser；需要生产资源语义时，通过 TranslationUnit / production validation capability 进入；
- 同时保留独立 fixture oracle，避免“production implementation 用自己生成的 expected value 测自己”。

共享 EvalCase v1 契约已经由 #32 / #33 冻结。当前 #13 与 #15 按已有 scope 推进；在本次 roadmap 校准完成前，不修改 #13 contributor contract。

### 3.2 Deterministic TM Audit — #8

TM audit 分成两个 domain：

**TM-internal audit** 可以直接检查：

- SQLite schema / integrity / WAL / backup readiness；
- formal / shadow / review / provenance 状态；
- authority 组合、内部冲突、重复或不一致记录。

**Project-correlated audit** 需要复用现有 application evidence：

- 当前 source fingerprint drift；
- orphan / unreachable coordinate；
- coverage / match-scope / pending delta；
- 与当前 TranslationUnit / TranslationPlan / QA 相关的异常。

TM Auditor 不应自己实现 PO/YAML/JSON parser。

该能力已有历史实现和测试资产，应以 **legacy behavior parity + 当前架构适配** 为目标，而不是从零另写第二套审计器。旧代码作为 executable specification / regression oracle 使用；最终实现仍复用当前 TM、Adapter、Planner、QA 等 application capability。

## 4. Track B — Controlled Intelligence

Controlled Intelligence 只用于确实存在开放式证据综合、方案比较或动态下一步选择的地方。

未来新增智能能力前必须同时满足：

1. 任务包含实质性的开放式判断，而不是固定顺序；
2. 模型错误能够被确定性校验或权限边界约束；
3. 相比现有 Dashboard / CLI，它能明显降低操作者认知成本。

不满足时，使用普通 application logic。

### 4.1 Prompt Workbench — #10

当前最有潜在价值的智能使用场景。

推荐工作流：

```text
inventory / lint
→ reviewable patch proposal
→ before / after Eval
→ evidence
→ human accept / reject
→ baseline lock
```

边界：

- Prompt Workbench 不直接解析 PO/YAML/JSON 等资源文件；
- format-specific translation context 只能由 Adapter / TranslationUnit / application capability 暴露；
- Python Prompt composition 保持协议权威；
- deterministic hard regression 不能被模型偏好覆盖；
- 第一版应先证明非 Agent CLI/service 路径有用，再决定是否加入 proposal orchestration。

### 4.2 TM Repair Advisor — #31

这是 #8 的可选后续，不是默认必交付能力。

只有当 deterministic findings 中确实存在需要开放式归因、分组、优先级或多方案 repair reasoning 的问题时才引入模型。

正常路径：

```text
tm-audit.json
→ reviewable repair proposal
→ explicit Apply approval
→ TM repository mutation
→ re-audit / re-plan / QA
→ normal Build
→ Adapter.render
```

TM repair 不直接修改 source/resource 文件。资源变化只能经正常 TranslationPlan / Build / Adapter render 路径发生。

### 4.3 Governed orchestration — #7

#7 不应成为 Quality Engineering 的前置条件，也不应因为 roadmap 中存在多个 Advisor 就自动扩成通用 Harness。

如果 #10 / #31 等真实 workflow 出现重复的 plan / approval / event / recovery 需求，再抽取最小共享协议：

```text
LLM/planner
→ reviewable proposal/plan
→ allowlisted application tool
→ deterministic service
→ verification
```

权限仍分 Inspect / Propose / Apply / Release / Publish，并由 Python application services fail closed。

### 4.4 Release Readiness Advisor — #11

保持 P2 / usage-driven。

第一阶段只做 evidence synthesis：解释当前为什么被阻断、真正的 blocker 是什么、最小安全恢复路径是什么。

它消费现有 ResourceScanner / Planner / TM audit / Eval / QualityGate / artifact / publish receipt 等证据，不自行解析资源格式，不重建 Dashboard 固定流程，更不默认发展成 Build Agent。

如果它只是重述 Prepare → Preflight → Run → Validate → Repair → Build → Publish，则不应继续扩张。

## 5. Track C — Productization

### Windows Tauri client — #12

Tauri 是独立产品化路线，与 Agent 是否存在无直接依赖。

最薄技术验证：

```text
Tauri
→ packaged Python sidecar
→ 127.0.0.1 random port
→ versioned health/session handshake
→ existing Dashboard
```

Tauri 负责窗口、原生文件选择、单实例、sidecar 生命周期、安装/升级等客户端责任；Python application services 继续拥有 localization、TM、QA、Build、Release、Publish 语义。

首个 prototype 只证明 packaging / lifecycle feasibility。没有用户价值证据时，不因为“完成 M8”而继续扩大桌面客户端范围。

## 6. Adapter Conformance 与跨项目验证

M8 之前的讨论暴露出一个更基础的工程需求：正式支持的 Adapter 应有统一行为契约和 conformance coverage。

当前公开格式边界保持不变：

- Gettext PO/MO；
- ParaTranz JSON；
- Paradox YAML。

在主动增加新格式前，应优先验证这些 Adapter 的：

- identity stability / collision behavior；
- projection semantics；
- no-op semantic round-trip；
- single-unit partial update；
- unrelated structure preservation；
- destination planning；
- render validation。

外部游戏首先作为 static compatibility corpus / falsification target，而不是需求生成器。一个项目使用 `.pot`、Fluent、Qt TS 或私有 placeholder syntax，不自动意味着当前 roadmap 必须支持它。

只有在以下情况之一成立时才升级为 implementation work：

1. 当前公开支持存在真实缺陷；
2. 两个或更多真实项目反复需要同一个通用能力；
3. 选定的跨项目验证目标无法在现有 Adapter contract 下完成，而且能提出无游戏硬编码的通用改进；
4. 项目主动决定扩大 public support boundary。

## 7. Roadmap 依赖关系

推荐理解为并行 track，而不是单链：

```text
Core localization architecture
        │
        ├── Adapter Contract / Conformance
        │
        ├── Quality Engineering
        │      ├── #13 / #15
        │      ├── #9 Eval Harness
        │      └── #8 TM Audit migration
        │
        ├── Controlled Intelligence (conditional)
        │      ├── #10 Prompt Workbench
        │      ├── #31 TM Repair Advisor
        │      ├── #7 minimal shared orchestration if duplication appears
        │      └── #11 Release Readiness Advisor
        │
        └── Productization
               └── #12 Tauri
```

## 8. Stop conditions

M8 不以“所有 Issue 都实现”作为成功条件。

允许以下结果成为正确决策：

- Eval + TM audit 提供了足够价值，因此不实现更多 Agent；
- Prompt Workbench 只保留 deterministic 工具，不发展成 conversational Agent；
- TM findings 大多可确定性修复，因此不实现 #31 的 LLM advisor；
- Release Readiness 没有比 Dashboard 增加可测价值，因此 #11 保持不实现；
- Tauri thin POC 证明用户收益不足，因此暂停产品化。

项目成熟度也包括有证据地决定 **不实现** roadmap 中的假设功能。

## 9. 文档与 Issue 的责任

本文只维护稳定架构原则、track 关系和决策规则。

具体 schema、acceptance criteria、实现切片和贡献者 scope 以对应 GitHub Issue 为准，避免设计文档复制 Issue 后产生漂移。
