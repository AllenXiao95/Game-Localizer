# Game Localizer 文档

**中文文档** | [English documentation](en/README.md)

这套文档面向第一次接触项目的使用者。内容从旧的单游戏脚本工作流中提炼，但示例只使用通用目录、语言和资源名，不依赖任何具体游戏的安装结构、文件名或发布平台。

建议按下面的顺序阅读：

1. [首次安装与第一次运行](getting-started.md)
2. [通用使用指南](usage.md)
3. [`project.yaml` 配置说明](project-configuration.md)
4. [从存量汉化资源构建 TM](tm-bootstrap.md)
5. [TM 与 SQLite 指南](translation-memory.md)

规划中的工程里程碑：

- [M8 Agent 化工作流与 Tauri 客户端设计](milestone-m8-agent-client.md)

如果只想解决常见问题：

- 启动前是否需要 venv/Conda：见[首次安装](getting-started.md#创建隔离环境)。
- `transformers is not installed`：见[选择安装方式](getting-started.md#选择安装方式)。
- SQLite 要不要手工建表：见[新项目如何创建 SQLite TM](translation-memory.md#新项目如何创建-sqlite-tm)。
- 旧 TM JSON 如何转成 SQLite：见[旧 JSON TM 转 SQLite](translation-memory.md#旧-json-tm-转-sqlite)。
- 只有存量汉化资源、没有 TM：见[从存量汉化资源构建 TM](tm-bootstrap.md)。
- 不理解某个 YAML 字段：见[配置字典](project-configuration.md#配置字典)。
