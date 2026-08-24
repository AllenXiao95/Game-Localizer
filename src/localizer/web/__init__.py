"""观测面板与受控本地任务启动器。

按 framework-design.md §16.4，面板提供 QA 缺陷的**单人定点修复**（编辑落本地 TM
并留完整审计），但不组织人工审核流程：没有审核队列、任务分发或多人审批。
本模块读取和展示运行状态，并允许保存无凭据预设、启动既有本地流水线；不提供任何
人工编辑、审批或 ParaTranz stage 变更能力。
人工翻译、校对与审核统一在 ParaTranz 完成（M5）。

它对应的是 §8.5 可观测性：每次运行的配置快照、进度、QA 报告、制品清单和日志
都应当可查。
"""

from .collector import DashboardCollector
from .dashboard_server import DashboardServer, serve

__all__ = ["DashboardCollector", "DashboardServer", "serve"]
