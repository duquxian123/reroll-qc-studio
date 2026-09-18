"""检测项注册表。

新增一个质检项只需要：
  1. 在 checks/ 下新建一个模块
  2. 定义类并 `register(YourCheck())`
  3. 在 checks/__init__.py 里 import 它

主流程、前端、判定逻辑都不用改。
"""

from __future__ import annotations

from .base import BaseCheck

# 默认执行顺序：先硬性项、先便宜的项。
DEFAULT_ORDER: list[str] = [
    "spec",
    "blackframe",
    "freeze",
    "flicker",
    "noise",
    "sharpness",
    "face",
    "hand",
]

_REGISTRY: dict[str, BaseCheck] = {}


def register(check: BaseCheck) -> BaseCheck:
    if check.id in _REGISTRY:
        raise ValueError(f"检测项 id 重复：{check.id}")
    _REGISTRY[check.id] = check
    return check


def all_checks() -> list[BaseCheck]:
    """按默认顺序返回全部已注册的检测项。"""
    ordered = [cid for cid in DEFAULT_ORDER if cid in _REGISTRY]
    extra = [cid for cid in _REGISTRY if cid not in DEFAULT_ORDER]
    return [_REGISTRY[cid] for cid in ordered + extra]


def get_check(check_id: str) -> BaseCheck:
    if check_id not in _REGISTRY:
        raise KeyError(f"未知检测项：{check_id}")
    return _REGISTRY[check_id]


def describe_all() -> list[dict]:
    """给前端的检测项目录。"""
    return [
        {
            "id": c.id,
            "name": c.name,
            "description": c.description,
            "unit": c.unit,
            "hard": c.hard,
            "default_weight": c.default_weight,
            "default_threshold": c.default_threshold,
            "pass_when_above": c.pass_when_above,
            "requires_models": c.requires_models,
            "default_enabled": getattr(c, "default_enabled", True),
        }
        for c in all_checks()
    ]
