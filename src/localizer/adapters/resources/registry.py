"""资源 Adapter 注册表。

M7 的验收标准之一是「新 Adapter 不需要修改翻译内核」。此前
`project_runner._resources()` 写死 `if configured.type != "gettext": raise`，
内核还直接 import 具体 Adapter —— 加一种格式要改内核好几处，这条验收无从谈起。

有了注册表 + `@register_adapter` 装饰器 + 包内自动发现，新增一种格式只需要
往 `adapters/resources/` 放一个新 .py 文件，连 `__init__.py` 都不用改。
"""
from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from localizer.adapters.resources.options import AdapterOptionsModel, normalize_options
from localizer.ports.resource import ResourceAdapter

ADAPTER_FACTORIES: Dict[str, Callable[..., ResourceAdapter]] = {}


def register_adapter(cls):
    """把 Adapter 类按其 adapter_id 登记进注册表。"""
    adapter_id = getattr(cls, "adapter_id", None)
    if not adapter_id:
        raise TypeError(f"{cls.__name__} must define a non-empty adapter_id")
    existing = ADAPTER_FACTORIES.get(adapter_id)
    if existing is not None and existing is not cls:
        raise ValueError(
            f"adapter_id {adapter_id!r} already registered by {existing.__name__}"
        )
    ADAPTER_FACTORIES[adapter_id] = cls
    return cls


def build_adapter(
    type_name: str,
    *,
    project_id: str,
    source_root: Path,
    source_locale: str,
    target_locale: str,
    options: Optional[Mapping[str, Any]] = None,
) -> ResourceAdapter:
    discover()
    factory = ADAPTER_FACTORIES.get(type_name)
    if factory is None:
        raise ValueError(
            f"unknown resource adapter {type_name!r}; available: "
            f"{', '.join(sorted(ADAPTER_FACTORIES)) or '(none)'}"
        )
    validated_options = validate_adapter_options(type_name, options or {})
    return factory(
        project_id=project_id,
        source_root=source_root,
        source_locale=source_locale,
        target_locale=target_locale,
        options=validated_options,
    )


def validate_adapter_options(type_name: str, options: Mapping[str, Any]) -> dict:
    """按 Adapter 自己声明的 Schema 校验并补齐默认值。"""
    discover()
    factory = ADAPTER_FACTORIES.get(type_name)
    if factory is None:
        raise ValueError(f"unknown resource adapter {type_name!r}")
    model_type = getattr(factory, "options_model", AdapterOptionsModel)
    return normalize_options(model_type, options)


def available_adapters() -> tuple:
    discover()
    return tuple(sorted(ADAPTER_FACTORIES))


_discovered = False


def discover() -> None:
    """导入 adapters/resources 包内的全部模块，触发装饰器登记。

    幂等：注册表本身对重复登记同一个类是允许的（见 register_adapter），
    但重复 import 没有意义，这里用一个标志位短路。
    """
    global _discovered
    if _discovered:
        return
    _discovered = True
    package = __name__.rsplit(".", 1)[0]
    for module in pkgutil.iter_modules([str(Path(__file__).parent)]):
        if module.name in {"registry", "__init__"}:
            continue
        importlib.import_module(f"{package}.{module.name}")
