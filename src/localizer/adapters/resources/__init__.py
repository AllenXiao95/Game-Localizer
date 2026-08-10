"""资源 Adapter 包。

导入本包即触发注册表的自动发现，因此新增 Adapter 只要往这个目录放一个
新模块并挂 @register_adapter，不需要改这里，也不需要改任何内核代码。
"""
from .registry import ADAPTER_FACTORIES, available_adapters, build_adapter, discover, register_adapter

discover()

__all__ = [
    "ADAPTER_FACTORIES",
    "available_adapters",
    "build_adapter",
    "discover",
    "register_adapter",
]
