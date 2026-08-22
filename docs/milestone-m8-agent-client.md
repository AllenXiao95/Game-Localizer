# M8：Agent 化工作流与 Tauri 客户端设计

**中文** | [English](en/milestone-m8-agent-client.md)

- 状态：设计提案
- 目标版本：M8
- 实现状态：尚未开始
- 适用范围：本地单人游戏本地化流水线

## 1. 背景与结论

Game Localizer 已经具备资源扫描、标准词条抽象、SQLite TM、模型辅助翻译、确定性 QA、
QualityGate、人工修订、制品构建和发布能力。Dashboard 也已经通过本地 HTTP API 暴露
任务预检、运行、审查、重建和发布入口。

M8 不重写这些业务内核，而是在其上增加两层能力：

1. 受控 Agent 编排层，把 TM 校验、Prompt 材料构建、评估和构建流程组织为可计划、
   可暂停、可恢复、可审计的任务；
2. Tauri 桌面外壳，以打包后的 Python 程序作为 sidecar，复用现有 WebUI 和本地 API，
   让最终用户无需安装 Python 或使用命令行。

核心决策如下：

- Python 内核继续是配置、TM、QA、QualityGate、构建和发布语义的唯一权威；
- Agent 只调用白名单工具，不获得任意 shell、任意路径读写或裸 SQLite 权限；
- 模型判断只提供建议或评估信号，不替代占位符、术语、格式和发布门禁等确定性判据；
- TM 正式写入、release 和 publish 都必须经过独立的确定性校验与显式人工授权；
- 客户端采用 Tauri 管理窗口、原生对话框、单实例、sidecar 生命周期、安装和升级；
- WebUI 默认继续由 Python sidecar 提供，以保持同源 API 和最大程度复用现有实现。

## 2. 目标与非目标

### 2.1 目标

- 提供 TM 体检 Agent，发现过期、冲突、重复、孤立、格式错误和权威来源异常，并生成
  可审阅的修复提案；
- 提供 Prompt 工作台，辅助创建和维护 `prompt.md`、`background.md`、`glossary.yaml`
  与 `rules.yaml` 的基础内容；
- 建设或合规复用一套 i18n Prompt 评估集，使 Prompt 和模型变更可以重复比较；
- 提供构建 Agent，编排 validate、scan、plan、preview、QA、rebuild、release 和 publish，
  并用结构化证据解释每次阻断；
- 发布基于 Tauri + Python sidecar 的 Windows 客户端，并为 macOS/Linux 保留清晰的
  构建边界；
- 保持 CLI、Dashboard 和桌面客户端共用同一套应用服务，避免三套业务实现漂移。

### 2.2 非目标

- 不在 M8 建设带账号、权限、任务分配和多人审批的在线协作平台；
- 不允许 Agent 自主接受存量债、晋升术语、覆盖人工定稿译文或直接发布制品；
- 不用 LLM judge 取代现有 QualityGate；
- 不把 Provider 密钥、发布凭据或压缩密码写入 Prompt、项目 YAML、Agent 日志或 TM；
- 不在首个客户端安装包中内置大模型或 Hugging Face 模型权重；
- 不要求一次完成所有操作系统，首个正式目标为 Windows x86-64。

## 3. 总体架构

```text
┌──────────────────────── Tauri desktop ────────────────────────┐
│ 窗口 / 菜单 / 原生文件选择 / 单实例 / 凭据入口 / 更新与签名 │
│                         │                                     │
│                 启动、握手、健康检查                          │
└─────────────────────────┼─────────────────────────────────────┘
                          │ stdin/stdout 控制通道
┌─────────────────────────▼─────────────────────────────────────┐
│              packaged Python sidecar                         │
│  DashboardServer / Agent Orchestrator / application services │
│          │                    │                    │           │
│       Web API          deterministic tools       audit log    │
└──────────┼────────────────────┼────────────────────┼───────────┘
           │                    │                    │
      existing WebUI      TM / QA / build      run artifacts
                               │
                 Provider / local publish / remote publish
```

Tauri 不复制 Python 领域逻辑。除窗口、文件选择、凭据桥接、sidecar 管理和客户端升级外，
所有业务操作都通过版本化的本地 API 完成。

### 3.1 Sidecar 启动协议

建议增加专用入口：

```text
localizer-sidecar desktop-server --config <project.yaml> --port 0
```

协议要求：

1. Tauri 启动 sidecar，并通过 stdin 或受控 IPC 传入本次会话材料；秘密不得出现在命令行
   参数、URL 或持久化日志中；
2. sidecar 绑定 `127.0.0.1` 随机端口；
3. sidecar 在 stdout 输出一条机器可读握手记录，至少包含 `protocol_version`、端口、PID
   和项目身份；后续业务日志写 stderr 或结构化日志文件，不能污染握手通道；
4. Tauri 调用 `/api/health`，确认 API 协议和项目配置已经就绪后再展示主窗口；
5. WebUI 通过 Tauri IPC 获得短生命周期会话能力，并在写请求中携带；不把令牌放进查询
   字符串或本地存储；
6. 退出时先查询运行状态。没有任务时优雅关闭；有任务时由用户选择继续驻留、等待完成或
   确认中止，不得静默杀死正在写 checkpoint/TM 的进程；
7. Tauri 异常退出后，sidecar 应检测父进程消失并执行有界收尾；下一次启动沿用现有
   owner lock、task snapshot 和 checkpoint 规则恢复。

### 3.2 本地安全边界

- 只允许回环地址，发布版不提供 `--host 0.0.0.0`；
- 每次启动生成新的会话能力，所有写 API 验证能力和既有动作头；
- WebView 禁止任意外部导航，新窗口和外部链接交给系统浏览器并使用允许列表；
- Tauri 文件对话框返回的路径仍由 Python 执行 `resolve(strict=True)` 和作用域校验；
- sidecar API 增加版本号，客户端与 sidecar 协议不匹配时 fail closed；
- 凭据由操作系统凭据设施或经评审的安全存储保存，只在单次任务执行期间注入 sidecar；
- Agent 工具层不接受任意 SQL、Python 表达式、shell 命令或未约束路径。

## 4. Agent 运行模型

这里的 Agent 是“模型规划 + 确定性工具 + 状态机”的组合，不是拥有机器控制权的聊天
机器人。每个 AgentRun 都必须产生不可变计划和追加式事件记录。

### 4.1 公共状态机

```text
draft → planned → awaiting_approval → running → verifying → completed
                   │                  │          │
                   └→ rejected        ├→ paused  └→ failed
                                      └→ failed
```

每一步至少记录：

- `agent_run_id`、Agent 类型和协议版本；
- 项目、资源变体、游戏版本和操作者；
- 配置修订、源资源指纹、TM 修订、Prompt 修订和评估集版本；
- 模型、Provider、采样参数、token/费用和工具调用；
- 输入/输出摘要、产生的文件、确定性校验结果和人工决策；
- 恢复点、失败分类和下一步建议。

Agent 输出不得只剩自然语言。每个计划、修复提案和执行结果都必须有版本化 JSON schema。

### 4.2 工具权限等级

| 等级 | 示例 | 默认授权 |
| --- | --- | --- |
| Inspect | 配置校验、扫描、TM 查询、QA 汇总、Prompt lint | 自动，只读 |
| Propose | 生成 TM patch、Prompt patch、构建计划 | 自动，只写隔离提案目录 |
| Apply | 应用 TM/Prompt 修复 | 人工确认，先备份并复核指纹 |
| Release | 正式构建、接受债务 | 每次显式确认；接受债务可要求更高权限 |
| Publish | 上传远端目标 | 与 release 分离，每个目标单独确认 |

模型不能把一个低等级工具调用转换成高等级动作。权限判断由 Python 应用服务执行，而不是
由 Prompt 约束保证。

## 5. TM 校验 Agent

### 5.1 只读检查范围

TM Agent 首轮只做体检，至少覆盖：

- SQLite schema、完整性检查、WAL 状态和可恢复备份能力；
- 同一稳定坐标下的源文指纹漂移、过期 formal 条目和 shadow/formal 状态异常；
- 同源多译、同坐标冲突、重复记录和不可达/孤立记录；
- 人工定稿、机器候选、历史迁移和 ParaTranz 回流之间的权威优先级；
- 占位符、控制字符、源语言残留、规范化和已审核术语违规；
- 记录的 run、model、prompt hash、审核者和来源是否可追溯；
- 当前资源扫描结果与 TM 覆盖率、命中范围和待翻译量的变化。

检查输出统一为 `tm-audit.json`，问题项必须带稳定身份、严重度、证据、建议动作和是否可以
自动生成修复提案。

### 5.2 修复提案与应用

TM Agent 不直接修改数据库，而是先生成 `tm-repair-plan.json`：

- 计划绑定数据库文件身份、事务前 revision、资源指纹和配置修订；
- 每个动作声明前置值、目标值、理由、来源和回滚信息；
- formal 退休、权威切换、批量统一和删除类动作必须单独分组；
- 应用前使用 SQLite backup API 生成一致性备份；
- 应用时重新核验计划指纹，并在单个事务内执行；
- 应用后重跑 TM audit、translation plan 和相关 QA；验证失败则回滚事务并保留报告；
- 不允许覆盖较高权威等级的人工作品，除非用户通过专用治理入口明确授权。

首版可以自动提出但不能自动应用删除、权威切换和存量债接受。

## 6. Prompt 基础设施与设计 Agent

### 6.1 Prompt 材料边界

Prompt Agent 管理的是项目级基础材料：

- `prompt.md`：目标语言、文风、称谓、命名和格式要求；
- `background.md`：世界观、UI 场景、角色关系和语境；
- `glossary.yaml`：结构化、可审核、可限定作用域的术语；
- `rules.yaml`：过滤、规范化和确定性 QA 规则。

批次编号、输出协议、实际待译文本和结构化术语注入继续由 `PromptComposer` 等 Python 代码
组装，避免每个项目复制协议。Agent 不把运行数据、凭据或整份评估答案写进项目 Prompt。

### 6.2 工作流

```text
项目元数据/样本 → 材料盘点 → scaffold → lint → eval → diff → 人工接受 → versioned files
```

Prompt Agent 应支持：

- 从 locale、资源 adapter、少量代表性文本和用户给定风格目标生成初始材料；
- 识别 Prompt、background、glossary 和 rules 之间的重复或冲突；
- 将可确定性表达的约束下沉为 glossary/rules，而不是继续堆进自然语言 Prompt；
- 生成文件级 patch，不静默覆盖现有内容；
- 对变更前后执行相同评估集，展示质量、成本和延迟差异；
- 保存 Prompt bundle manifest，记录所有材料摘要和组装器版本；
- 允许锁定已发布 Prompt 基线，并在 release manifest 中保留其修订身份。

## 7. i18n Prompt 评估集

### 7.1 建设与复用原则

M8 预计“复用公开兼容数据 + 自建合成与回归样本”形成评估集，而不是直接复制未知许可的
游戏文本。引入任何外部数据前必须记录：

- 原始来源、版本、许可证、允许的使用和再分发范围；
- 语言对、领域、数据清洗和转换方法；
- 是否含个人信息、未公开文本或受限制内容；
- 与项目真实分布的差异和已知偏差。

无法确认许可或来源的数据只能用于本地临时研究，不能进入仓库或公开基准。真实项目回归
样本必须经过授权和脱敏；公开仓库中的默认集优先使用原创合成、公共领域或明确兼容许可的
文本。

### 7.2 评估维度

- 响应协议：编号、数量、终止标记、JSON/文本结构完整；
- 占位符与标记：printf、Python、ICU、XML/BBCode、转义、换行和游戏自定义 token；
- 术语：强制术语、禁用译法、大小写、词形变化和路径 scope；
- 语义：遗漏、增译、否定、数值、单位、实体和指代；
- 本地化习惯：标点、空格、数字、日期、复数、敬语、性别和 locale 特有格式；
- UI 约束：按钮短文本、宽度预算、菜单一致性和快捷键标记；
- 文体与语域：角色口吻、系统提示、叙事文本和年龄分级；
- 一致性：同源文本、跨文件实体、上下文变体和历史基线；
- 鲁棒性：提示注入样文本、混合语言、异常 Unicode、长文本和不完整上下文；
- 成本与性能：输入/输出 token、延迟、失败率、修复重试和批次吞吐。

### 7.3 建议数据格式

```json
{
  "schema_version": 1,
  "id": "placeholder.printf.001",
  "source_locale": "en-US",
  "target_locale": "zh-Hans",
  "domain": "game-ui",
  "source": "Welcome, %s! You have %d coins.",
  "context": {"screen": "login-reward", "max_chars": 40},
  "glossary": [],
  "required_tokens": ["%s", "%d"],
  "references": ["欢迎你，%s！你有 %d 枚金币。"],
  "assertions": ["protocol", "placeholder_set", "number_fidelity"],
  "provenance": {"kind": "synthetic", "license": "CC0-1.0"},
  "difficulty": "basic",
  "tags": ["ui", "printf"]
}
```

建议目录：

```text
evals/i18n-prompt/
├── manifest.yaml
├── cases/
│   ├── protocol.jsonl
│   ├── placeholders.jsonl
│   ├── terminology.jsonl
│   ├── semantics.jsonl
│   └── locale-style.jsonl
├── rubrics/
│   └── semantic-fidelity.yaml
└── baselines/
    └── <prompt-bundle>-<provider-model>.json
```

### 7.4 评分与门禁

评分分三层：

1. 确定性断言：协议、占位符、数字、长度、术语和格式；
2. 参考与规则评分：规范化匹配、允许的多参考答案、字符/词级差异；
3. 模型裁判与人工抽样：只评估难以确定性判断的语义、语域和风格。

任何模型裁判结果都要记录裁判模型、Prompt、参数和原始理由，并通过固定样本进行校准。
首版模型裁判只生成报告，不直接阻断 release。正式门禁优先使用确定性指标，例如：

- 协议完整率和占位符保留率必须为 100%；
- 数值/实体硬错误不得新增；
- 相对已接受基线，确定性失败数不得回退；
- 语义/风格分数低于基线时提示人工复核，而不是自动改写正式 TM。

开发集可用于迭代 Prompt；锁定测试集不得注入 Prompt、background 或 few-shot 示例，避免
针对答案过拟合。报告必须同时比较质量、成本和延迟，不能只追求单一总分。

## 8. 构建 Agent

构建 Agent 复用现有 ProjectRunner 和 Web task service，不重新实现流水线。建议工具拆分：

| 工具 | 权限 | 产物 |
| --- | --- | --- |
| `project.validate` | Inspect | 配置诊断 |
| `resource.scan` | Inspect | 扫描清单与资源指纹 |
| `tm.audit` | Inspect | TM 体检报告 |
| `translation.plan` | Inspect | 待译计划与成本估算 |
| `prompt.evaluate` | Inspect | 评估报告与基线 diff |
| `build.preview` | Apply | preview、QA 和 checkpoint |
| `review.propose` | Propose | 修订提案，不写 formal TM |
| `build.release` | Release | release manifest 与制品 |
| `publish.prepare` | Inspect | 目标、凭据状态和回读计划 |
| `publish.execute` | Publish | 单目标发布结果 |

推荐执行顺序：

```text
validate → scan → TM audit → translation plan → prompt baseline check
         → preview → QA → 人工修订/增量 rebuild
         → release preflight → 人工确认 → release
         → publish preflight → 逐目标确认 → publish + read-back
```

约束：

- Agent 必须先给出计划、预计模型调用量、可能写入的位置和不可逆动作；
- preview 可以在用户批准一个工作流后自动执行，release/publish 不能沿用模糊的长期授权；
- 计划执行前后都校验 source/config/TM/Prompt 指纹，漂移时重新规划；
- QualityGate 失败只能进入解释、修订提案或 rebuild，不能由 Agent 降级严重度绕过；
- publish 与 release 分成两个授权域，多个发布目标逐个返回成功、失败和可重试状态；
- 任务恢复继续复用 checkpoint，不重新支付已经成功完成的模型请求。

## 9. 客户端用户体验

首个客户端版本至少提供：

- 欢迎页：打开 `project.yaml`、最近项目、移除失效记录；
- 项目页：复用现有 Dashboard，并增加原生源目录、单文件和 `.env` 选择按钮；
- Agent 中心：显示计划、权限等级、预计成本、当前步骤、证据和待确认动作；
- Prompt 工作台：材料 diff、lint、评估集选择、基线比较和 patch 接受；
- TM 体检：问题聚类、修复提案、备份位置和应用后验证；
- 任务托盘：长任务后台运行、重新打开窗口和退出确认；
- 诊断包：显式选择后导出脱敏日志、版本和环境状态，不包含凭据与原始私有文本；
- About/更新页：客户端、sidecar、API 协议和数据 schema 版本。

用户数据建议遵循各平台应用数据目录，而不是安装目录：

```text
<app-data>/game-localizer/
├── client.json
├── logs/
├── diagnostics/
└── updates/
```

项目 TM、workspace 和 output 仍由 `project.yaml` 决定，客户端不得在升级时迁移或删除它们。

## 10. 代码与产物布局提案

```text
src/localizer/agent/
├── models.py          # AgentRun、计划、权限和事件 schema
├── orchestrator.py    # 状态机、恢复和审批
├── tools/
│   ├── tm.py
│   ├── prompt.py
│   └── build.py
└── audit.py            # 追加式审计事件

src/localizer/web/
├── server.py           # /api/agent/*、/api/health、会话验证
└── static/index.html   # Agent/Prompt/TM UI，后续可拆分静态资产

src-tauri/
├── src/                # sidecar 生命周期、窗口、托盘和原生能力
├── capabilities/       # 最小权限 allowlist
└── tauri.conf.json

evals/i18n-prompt/      # 合规评估集、rubric 与基线
```

所有跨 Tauri/Python 边界的数据都必须有 schema 和 `protocol_version`；Tauri 只理解客户端
控制协议及通用任务状态，不理解 TM 分类或 QualityGate 细节。

## 11. 阶段拆分

### M8.1：协议与 Agent 基础

- 定义 AgentRun、计划、事件、审批和工具结果 schema；
- 增加追加式 Agent 审计日志与恢复状态机；
- 为现有应用服务建立白名单工具适配层；
- 增加 API 版本、健康检查和会话能力；
- 用 fake planner/provider 覆盖权限升级、指纹漂移和恢复测试。

### M8.2：Prompt 工作台与评估集

- 完成 Prompt bundle manifest 和 lint；
- 完成评估集 manifest、JSONL schema、许可检查和数据版本；
- 先建立协议、占位符、数值、术语四类确定性样本；
- 接入语义/风格 rubric 和人工抽样，不作为 release 硬门禁；
- 产出至少一个现有示例 Prompt 的可重复基线。

### M8.3：TM 与构建 Agent

- 实现只读 TM audit 和结构化修复提案；
- 实现备份、指纹复核、事务应用和应用后复检；
- 实现 preview 编排、QA 解释和增量 rebuild；
- release/publish 审批保持独立，并增加越权回归测试。

### M8.4：Tauri Windows 客户端

- PyInstaller/Nuitka 之一生成 Windows Python sidecar，构建方式固定并可复现；
- Tauri 完成 sidecar 握手、随机端口、窗口、文件对话框、单实例和托盘；
- 客户端不依赖系统 Python，安装包不内置 tokenizer 权重；
- 增加 Windows 安装、升级、卸载和异常退出冒烟测试；
- 完成代码签名、第三方许可清单和 GPL 对应源码交付流程。

### M8.5：跨平台与发布加固

- 增加 macOS、Linux sidecar 和客户端构建矩阵；
- 验证 WKWebView/WebKitGTK 下的 WebUI 行为；
- 完成 macOS 签名、公证以及各平台更新产物；
- 建立客户端/sidecar 协议兼容矩阵和回滚演练。

## 12. 验收标准

### Agent 与治理

- Agent 无法通过 Prompt 或工具参数执行任意 shell、SQL 或越界路径读写；
- Inspect/Propose 不修改项目正式数据；
- TM Apply 必须存在一致性备份、人工批准和未漂移计划指纹；
- Agent 不能覆盖更高权威的人工 TM，不能接受存量债，不能绕过 QualityGate；
- release 和每个远端 publish 都留下独立人工决策和追加式审计记录；
- Agent 进程中断后能从最后一个已提交步骤恢复，不重复已成功模型请求。

### Prompt 评估

- 评估集每条数据都有稳定 ID、版本、来源和许可字段；
- 锁定测试集与开发集分离，报告可证明测试答案没有进入被测 Prompt；
- 相同 Prompt bundle、模型快照/标识和参数能够生成结构一致的报告；
- 协议、占位符、术语和数值指标可由确定性代码复算；
- 基线报告同时包含质量、失败分类、token、费用和延迟；
- 模型裁判不可单独将失败结果晋升为可 release。

### 客户端

- 干净 Windows 环境无需安装 Python 即可打开项目和运行 preview；
- sidecar 使用随机回环端口，未持有本次会话能力的写请求被拒绝；
- 客户端和 CLI 对同一输入产生相同的 plan、QA、manifest 和 TM 结果；
- 运行中关闭窗口不会损坏 checkpoint/TM，用户可以恢复或继续后台运行；
- 安装、升级和卸载不删除项目 TM、workspace、output 或用户选择的游戏资源；
- 安装包有签名、版本、许可清单和对应源码获取方式；
- 现有完整测试集继续通过，并新增 sidecar 协议及客户端冒烟测试。

## 13. 风险与缓解

| 风险 | 缓解措施 |
| --- | --- |
| Agent 幻觉或越权 | 白名单工具、权限等级、schema 校验、确定性门禁、人工审批 |
| Prompt 针对评估集过拟合 | 开发/锁定测试分离、隐藏测试、类别级回归和真实人工抽样 |
| 外部数据许可不清 | 强制 provenance/license，默认使用原创合成或兼容许可数据 |
| TM 批量修复破坏权威数据 | SQLite 一致性备份、事务、指纹、权限优先级、应用后复检 |
| Tauri 与 sidecar 生命周期失配 | 版本化握手、父进程检测、健康检查、托盘策略、崩溃恢复测试 |
| Python 打包体积过大 | 按目标打包依赖、不内置模型权重、首版采用目录式 sidecar |
| WebView 平台差异 | Windows 先行，后续增加真实 macOS/Linux 构建和 UI 冒烟测试 |
| 自动升级损坏数据 | 更新程序只替换应用文件，项目数据外置，协议迁移可回滚 |

## 14. 开始实现前需要冻结的决策

M8.1 开工前需要通过设计评审确定：

1. Agent planner 的首个 Provider 接口和离线 fake 实现；
2. Agent 审计日志使用 JSONL 文件、SQLite 独立表，还是二者组合；
3. Tauri 与 sidecar 的会话能力传递方式和协议版本策略；
4. Windows sidecar 使用 PyInstaller 还是 Nuitka，以及目录式/单文件交付选择；
5. 凭据由 Tauri 安全存储还是 Python keyring 管理；
6. 首批公开 i18n 数据源的许可证审查结果和允许再分发范围；
7. 哪些确定性评估指标进入 Prompt 基线硬门禁；
8. 运行中关闭窗口的默认策略，以及“中止任务”的精确定义。

这些决策冻结前可以完成 schema 原型和只读评估，但不应提前实现正式 TM Apply、自动更新
或远端 publish Agent。
