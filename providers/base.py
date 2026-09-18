"""视频生成 Provider 的统一接口。

为什么要这层抽象
----------------
生成侧（即梦网页版 / Seedance API / 本地 ComfyUI）和质检侧是完全解耦的。
编排、质检、判定、排名只认这个接口，换后端不改逻辑：

    mock     —— demo 阶段用，从预置素材池取 / 本地 ffmpeg 合成
    seedance —— 火山方舟 Seedance，字段映射已写好，填 API key 即可用

注意三个和方舟对接时会踩的坑（已在 seedance.py 里处理）：
  1. 返回的是**临时签名 URL**，必须立刻下载落盘
  2. 是**异步任务**，需要轮询
  3. **seed 不一定能固定复现**，所以重抽不能靠改 seed，只能靠多抽
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class GenRequest:
    """一次生成请求。字段与分镜表的列一一对应。"""

    shot_id: str
    prompt: str
    negative_prompt: str = ""
    duration_s: float = 5.0
    aspect_ratio: str = "16:9"
    resolution: str = "720p"
    ref_image: str | None = None      # 首帧参考图（图生视频）
    seed: int | None = None
    watermark: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Capabilities:
    max_duration_s: float
    supports_seed: bool
    supports_image_to_video: bool
    supports_last_frame: bool
    supports_reference_images: bool
    resolutions: list[str]


@dataclass
class GenJobRef:
    """一次生成任务的句柄。"""

    provider: str
    job_id: str
    raw: dict[str, Any] = field(default_factory=dict)


class VideoProvider(Protocol):
    name: str

    def capabilities(self) -> Capabilities: ...

    def estimate_cost_cents(self, req: GenRequest) -> float:
        """估算成本，单位：分（人民币）。用于预算守卫。"""
        ...

    def submit(self, req: GenRequest) -> GenJobRef: ...

    def poll(self, ref: GenJobRef) -> str:
        """返回 queued / running / succeeded / failed / canceled。"""
        ...

    def fetch(self, ref: GenJobRef, dest: Path) -> Path:
        """把成品下载/落盘到 dest。临时 URL 必须在这一步立刻取走。"""
        ...
