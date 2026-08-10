"""Adapter 专属配置 Schema。

每个 Adapter 在注册时携带自己的 options_model。这样 project.yaml 仍保持统一的
`resources.adapters[]` 外形，但 options 不再是无法校验的任意字典；新增 Adapter 也只需
在自身模块声明 Schema，不需要修改 Application 层。
"""
from __future__ import annotations

from typing import Dict, Literal

from pydantic import BaseModel, Field, root_validator, validator


class AdapterOptionsModel(BaseModel):
    class Config:
        extra = "forbid"


class GettextOptions(AdapterOptionsModel):
    # standard: msgid=源文，msgstr=已有/目标译文（普通 PO/MO）。
    # keyed_source: msgid=稳定逻辑键，msgstr=源语言正文（WOT 等 keyed catalog）。
    layout: Literal["standard", "keyed_source"] = "standard"
    empty_source: Literal["skip", "error"] = "skip"
    source_filter: Literal["all", "cyrillic_without_cjk"] = "all"


class ParaTranzJsonOptions(AdapterOptionsModel):
    key_field: str = "key"
    source_field: str = "original"
    translation_field: str = "translation"
    context_field: str = "context"
    stage_field: str = "stage"
    id_field: str = "id"

    @validator(
        "key_field",
        "source_field",
        "translation_field",
        "context_field",
        "stage_field",
        "id_field",
    )
    def field_name_must_not_be_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("JSON field names must not be empty")
        return value

    @root_validator(skip_on_failure=True)
    def identity_fields_must_be_distinct(cls, values: dict) -> dict:
        names = [
            values.get("key_field"),
            values.get("source_field"),
            values.get("translation_field"),
        ]
        if len(set(names)) != len(names):
            raise ValueError("key/source/translation fields must be distinct")
        return values


class ParadoxYmlOptions(AdapterOptionsModel):
    locale_folders: Dict[str, str] = Field(default_factory=dict)

    @validator("locale_folders")
    def locale_folder_names_must_be_safe(cls, value: Dict[str, str]) -> Dict[str, str]:
        for locale, folder in value.items():
            if not locale.strip() or not folder.strip():
                raise ValueError("locale_folders cannot contain empty names")
            if any(part in folder for part in ("/", "\\", "..")):
                raise ValueError("locale folder must be one safe directory name")
        return value


def model_to_dict(model: BaseModel) -> dict:
    return model.model_dump() if hasattr(model, "model_dump") else model.dict()


def normalize_options(model_type, values) -> dict:
    model = (
        model_type.model_validate(dict(values or {}))
        if hasattr(model_type, "model_validate")
        else model_type.parse_obj(dict(values or {}))
    )
    return model_to_dict(model)
