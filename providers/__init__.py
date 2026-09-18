"""生成 Provider 包。"""

from __future__ import annotations

from .base import Capabilities, GenJobRef, GenRequest, VideoProvider
from .mock import MockProvider
from .seedance import SeedanceProvider

__all__ = [
    "Capabilities",
    "GenJobRef",
    "GenRequest",
    "VideoProvider",
    "MockProvider",
    "SeedanceProvider",
]
