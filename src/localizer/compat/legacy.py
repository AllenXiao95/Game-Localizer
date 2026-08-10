from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional, Sequence


class LegacyPhase(str, Enum):
    M1_M2 = "m1_m2"
    M3_SHADOW = "m3_shadow"
    SQLITE_AUTHORITY = "sqlite_authority"
    RETIRED = "retired"


# 由松到紧。派生出来的阶段是**上界**：人可以更严，不能更松。
_PERMISSIVENESS = {
    LegacyPhase.M1_M2: 0,
    LegacyPhase.M3_SHADOW: 1,
    LegacyPhase.SQLITE_AUTHORITY: 2,
    LegacyPhase.RETIRED: 3,
}


def phase_for_tm(tm) -> LegacyPhase:
    """从 TM 的实际状态推导旧入口所处阶段。

    原来阶段是 `localizer legacy --phase` 的一个**默认 M1_M2** 的命令行开关 ——
    也就是最宽松的一档。切完权威源之后照样可以 `localizer legacy --save-tm`
    整库覆盖 `history_tm.json`，`LegacyAccessPolicy` 的三条禁令一条都不会触发。
    阶段必须来自库的状态，不能来自调用者的自觉。
    """
    if tm.is_authoritative():
        return LegacyPhase.SQLITE_AUTHORITY
    if tm.shadow_sync_started():
        return LegacyPhase.M3_SHADOW
    return LegacyPhase.M1_M2


def resolve_phase(derived: LegacyPhase, requested: Optional[LegacyPhase]) -> LegacyPhase:
    """允许手动收紧，拒绝手动放松。"""
    if requested is None:
        return derived
    if _PERMISSIVENESS[requested] < _PERMISSIVENESS[derived]:
        raise RuntimeError(
            f"TM 状态对应的阶段是 {derived.value}，不允许降级到更宽松的 "
            f"{requested.value}；旧入口的写入边界由库的状态决定。"
        )
    return requested


# 旧入口 multi_i18n_processor_v6.py 的完整长选项集合。判断 --save-tm 时必须按
# argparse 的消歧规则来：它默认 allow_abbrev=True，任何**唯一匹配**的前缀都等价于
# 全名。原实现只做 `"--save-tm" in arguments` 字面量判断，于是 --save / --save-t /
# --sa / --s 全部绕过门面；--save-tm=1 形式同样漏判。
_LEGACY_LONG_OPTIONS = (
    "--env",
    "--version",
    "--save-tm",
    "--no-auto-glossary",
    "--glossary-min-frequency",
    "--force-release",
    "--help",
)
_TM_SAVE_OPTION = "--save-tm"


def _resolves_to(token: str, option: str) -> bool:
    """token 是否按 argparse 的前缀消歧规则解析为 option。"""
    if not token.startswith("--"):
        return False
    name = token.split("=", 1)[0]
    if name == option:
        return True
    matches = [opt for opt in _LEGACY_LONG_OPTIONS if opt.startswith(name)]
    return len(matches) == 1 and matches[0] == option


def _requests_tm_save(arguments: Sequence[str]) -> bool:
    return any(_resolves_to(token, _TM_SAVE_OPTION) for token in arguments)


@dataclass(frozen=True)
class LegacyAccessPolicy:
    phase: LegacyPhase

    @property
    def may_read_history_json(self) -> bool:
        return self.phase in {LegacyPhase.M1_M2, LegacyPhase.M3_SHADOW}

    @property
    def may_write_history_json(self) -> bool:
        return self.phase == LegacyPhase.M1_M2

    @property
    def requires_shadow_log(self) -> bool:
        return self.phase == LegacyPhase.M3_SHADOW

    def validate_arguments(self, arguments: Sequence[str]) -> None:
        saves_tm = _requests_tm_save(arguments)
        if self.phase == LegacyPhase.RETIRED:
            raise RuntimeError("legacy entry is retired")
        if self.phase == LegacyPhase.SQLITE_AUTHORITY and saves_tm:
            raise RuntimeError("legacy --save-tm is forbidden after SQLite becomes authoritative")
        if self.phase == LegacyPhase.M3_SHADOW and saves_tm:
            raise RuntimeError(
                "legacy --save-tm must use the M3 shadow-sync write protocol; "
                "unconstrained whole-file replacement is forbidden"
            )


class LegacyMainFacade:
    def __init__(self, repository_root: Path, policy: LegacyAccessPolicy) -> None:
        self.repository_root = Path(repository_root).resolve()
        self.policy = policy
        self.script = self.repository_root / "multi_i18n_processor_v6.py"

    def describe_boundary(self) -> dict:
        return {
            "phase": self.policy.phase.value,
            "may_read_history_json": self.policy.may_read_history_json,
            "may_write_history_json": self.policy.may_write_history_json,
            "requires_shadow_log": self.policy.requires_shadow_log,
            "legacy_script": str(self.script),
        }

    def run(self, arguments: Sequence[str]) -> int:
        self.policy.validate_arguments(arguments)
        if not self.script.is_file():
            raise FileNotFoundError(self.script)
        completed = subprocess.run(
            [sys.executable, str(self.script), *arguments],
            cwd=str(self.repository_root),
            check=False,
        )
        return completed.returncode
