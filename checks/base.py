"""质检项的基础设施：协议、结果结构、共享的视频读取缓存。

设计要点
--------
* 每个检测项都是一个独立的、可插拔的模块（见 checks/ 下的各文件）。
* 检测项之间**不互相依赖**，只依赖 `FrameCache` 提供的抽帧结果。
* 每个检测项都要产出**可解释的证据**（哪一帧、哪个指标、什么数值），
  否则人工无法复核，阈值也无从校准。
"""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np

# 抽帧上限：8 个检测项共享同一批帧，避免重复解码。
# 取 128 是为了让 5 秒 @25fps（125 帧）能**逐帧**覆盖——
# 子采样会把高频闪烁混叠掉，导致闪烁检测失灵。
MAX_SAMPLED_FRAMES = 128


@dataclass(frozen=True)
class VideoMeta:
    path: Path
    width: int
    height: int
    fps: float
    duration_s: float
    frame_count: int
    codec: str = "unknown"


@dataclass
class Evidence:
    """一条可复核的证据。

    kind:
      * ``frame``   —— 某一帧的原始画面
      * ``overlay`` —— 画了标注（如关键点骨架）的帧
      * ``chart``   —— 指标随帧变化的曲线图
    """

    kind: str
    label: str
    frame_index: int | None = None
    image_path: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckResult:
    check_id: str
    score: float  # 0-100，越高越好
    passed: bool
    detail: dict[str, Any] = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "score": round(self.score, 2),
            "passed": self.passed,
            "detail": self.detail,
            "notes": self.notes,
            "evidence": [
                {
                    "kind": e.kind,
                    "label": e.label,
                    "frame_index": e.frame_index,
                    "image_path": e.image_path,
                    "data": e.data,
                }
                for e in self.evidence
            ],
        }


class FrameCache:
    """按需抽帧 + 缓存。同一个视频被多个检测项复用时只解码一次。

    抽帧策略：在整段视频上**均匀采样** ``MAX_SAMPLED_FRAMES`` 帧。
    对 3-5 秒的短视频来说，这基本等于逐帧覆盖。
    """

    def __init__(self, path: Path, max_frames: int = MAX_SAMPLED_FRAMES) -> None:
        self.path = Path(path)
        self._max_frames = max_frames
        self._meta: VideoMeta | None = None
        self._frames: np.ndarray | None = None

    # -- 元数据 ---------------------------------------------------------
    @property
    def meta(self) -> VideoMeta:
        if self._meta is None:
            self._meta = self._probe()
        return self._meta

    def _probe(self) -> VideoMeta:
        cap = cv2.VideoCapture(str(self.path))
        if not cap.isOpened():
            raise IOError(f"无法打开视频：{self.path}")
        try:
            fps = float(cap.get(cv2.CAP_PROP_FPS)) or 25.0
            count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        finally:
            cap.release()

        duration = count / fps if fps > 0 else 0.0
        return VideoMeta(
            path=self.path,
            width=width,
            height=height,
            fps=fps,
            duration_s=duration,
            frame_count=count,
            codec=self._probe_codec(),
        )

    def _probe_codec(self) -> str:
        """有 ffprobe 就顺便读一下编码格式，没有就算了。"""
        try:
            out = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=codec_name",
                    "-of", "json", str(self.path),
                ],
                capture_output=True, text=True, timeout=15,
            )
            if out.returncode == 0:
                streams = json.loads(out.stdout or "{}").get("streams") or []
                if streams:
                    return streams[0].get("codec_name", "unknown")
        except Exception:  # noqa: BLE001 - 读不到编码格式不是致命问题
            pass
        return "unknown"

    # -- 抽帧 -----------------------------------------------------------
    @property
    def frames(self) -> np.ndarray:
        """返回 (N, H, W, 3) 的 BGR 帧数组，均匀采样。"""
        if self._frames is None:
            self._frames = self._sample()
        return self._frames

    def _sample(self) -> np.ndarray:
        cap = cv2.VideoCapture(str(self.path))
        if not cap.isOpened():
            raise IOError(f"无法打开视频：{self.path}")

        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        collected: list[np.ndarray] = []
        try:
            if total <= 0 or total <= 2000:
                # 短视频走**顺序解码**：边读边按步长保留。
                # 逐帧 cap.set(POS_FRAMES) 每次都要从关键帧重新解码，
                # 在 5 秒片段上比顺序解码慢一个数量级。
                step = max(1, math.ceil(total / self._max_frames)) if total > 0 else 1
                i = 0
                while len(collected) < self._max_frames:
                    ok, frame = cap.read()
                    if not ok:
                        break
                    if i % step == 0:
                        collected.append(frame)
                    i += 1
            else:
                # 长视频才用 seek，避免顺序解码浪费时间
                step = max(1, math.ceil(total / self._max_frames))
                idx = 0
                while idx < total and len(collected) < self._max_frames:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                    ok, frame = cap.read()
                    if not ok:
                        break
                    collected.append(frame)
                    idx += step
        finally:
            cap.release()

        if not collected:
            raise IOError(f"抽帧失败（读不到任何帧）：{self.path}")
        return np.stack(collected, axis=0)

    # -- 工具方法 -------------------------------------------------------
    def frame_index_of(self, sample_pos: int) -> int:
        """把采样序号换算回原始帧号，用于证据展示。"""
        meta = self.meta
        n = len(self.frames)
        if n <= 1 or meta.frame_count <= 0:
            return 0
        return int(round(sample_pos / (n - 1) * max(0, meta.frame_count - 1)))

    def gray_frames(self) -> np.ndarray:
        """灰度帧序列 (N, H, W)，float32。"""
        frames = self.frames
        return np.array(
            [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames], dtype=np.float32
        )

    def frames_subset(self, limit: int) -> np.ndarray:
        """均匀取 limit 帧。

        不需要时序密度的检测项（噪点、清晰度）用它来省时间；
        需要逐帧比较的检测项（卡帧、闪烁）必须用完整的 `frames`。
        """
        frames = self.frames
        if len(frames) <= limit:
            return frames
        idx = np.linspace(0, len(frames) - 1, limit).astype(int)
        return frames[idx]

    def gray_subset(self, limit: int) -> np.ndarray:
        return np.array(
            [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in self.frames_subset(limit)],
            dtype=np.float32,
        )


class BaseCheck(Protocol):
    """检测项协议。新增一项质检 = 新建一个模块 + 注册，不改主流程。"""

    id: str
    name: str
    description: str
    unit: str
    hard: bool  # True 表示一票否决
    default_weight: float
    default_threshold: float
    # 分数与阈值的比较方向：True 表示"分数 >= 阈值"才算通过
    pass_when_above: bool
    requires_models: bool

    def run(self, cache: FrameCache, params: dict[str, Any]) -> CheckResult: ...


def clamp_score(value: float) -> float:
    """把任意分数夹到 0-100。"""
    return float(max(0.0, min(100.0, value)))


def ramp(value: float, bad: float, good: float) -> float:
    """把原始指标线性映射到 0-100 分。

    ``bad`` 是"完全不能接受"的值，``good`` 是"完全没问题"的值。
    允许 bad > good（指标越小越好）。
    """
    if bad == good:
        return 100.0 if value == good else 0.0
    ratio = (value - bad) / (good - bad)
    return clamp_score(ratio * 100.0)
