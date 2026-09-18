"""质检项包。import 本包即完成全部检测项的注册。"""

from __future__ import annotations

from .base import (
    BaseCheck,
    CheckResult,
    Evidence,
    FrameCache,
    VideoMeta,
    clamp_score,
    ramp,
)
from .registry import DEFAULT_ORDER, all_checks, describe_all, get_check, register

# 导入即注册（顺序无关，最终顺序由 DEFAULT_ORDER 决定）
from . import spec  # noqa: E402,F401
from . import blackframe  # noqa: E402,F401
from . import freeze  # noqa: E402,F401
from . import flicker  # noqa: E402,F401
from . import noise  # noqa: E402,F401
from . import sharpness  # noqa: E402,F401
from . import face  # noqa: E402,F401
from . import hand  # noqa: E402,F401

__all__ = [
    "BaseCheck",
    "CheckResult",
    "Evidence",
    "FrameCache",
    "VideoMeta",
    "clamp_score",
    "ramp",
    "DEFAULT_ORDER",
    "all_checks",
    "describe_all",
    "get_check",
    "register",
]
