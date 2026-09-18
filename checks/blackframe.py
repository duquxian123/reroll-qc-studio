"""黑屏 / 纯色帧检测（硬性项）。

判据：一帧如果整体亮度极低、或者几乎没有任何像素差异（纯色），
就不是有效画面。AI 生成里常见于生成中断、结尾糊成一片。
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from .base import CheckResult, Evidence, FrameCache, ramp
from .registry import register


class BlackFrameCheck:
    id = "blackframe"
    name = "黑屏/纯色"
    description = "检测黑帧、纯色帧、生成中断导致的无画面。属于硬性缺陷。"
    unit = "分"
    hard = True
    default_weight = 1.0
    default_threshold = 95.0
    pass_when_above = True
    requires_models = False
    default_enabled = True

    def run(self, cache: FrameCache, params: dict[str, Any]) -> CheckResult:
        black_mean = float(params.get("black_mean_max", 16.0))
        black_std = float(params.get("black_std_max", 16.0))
        flat_std = float(params.get("flat_std_max", 6.0))

        frames = cache.frames
        # 用 OpenCV 自带的 meanStdDev：比 numpy 的 reshape+mean+std 快好几倍
        means = np.empty(len(frames), dtype=np.float32)
        stds = np.empty(len(frames), dtype=np.float32)
        for i, frame in enumerate(frames):
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            mean, std = cv2.meanStdDev(gray)
            means[i] = float(mean[0][0])
            stds[i] = float(std[0][0])

        is_black = (means < black_mean) & (stds < black_std)
        is_flat = stds < flat_std
        bad = is_black | is_flat

        bad_ratio = float(bad.mean())
        score = ramp(bad_ratio, bad=0.30, good=0.0)
        passed = bad_ratio <= 0.05

        detail = {
            "sampled_frames": int(len(frames)),
            "bad_frames": int(bad.sum()),
            "bad_ratio": round(bad_ratio, 4),
            "black_frames": int(is_black.sum()),
            "flat_frames": int(is_flat.sum()),
            "mean_brightness": round(float(means.mean()), 2),
            "mean_std": round(float(stds.mean()), 2),
        }

        evidence: list[Evidence] = []
        if bad.any():
            # 取最"平"的一帧作为证据
            worst = int(np.argmin(stds))
            evidence.append(
                Evidence(
                    kind="frame",
                    label=f"最异常的一帧（标准差 {stds[worst]:.1f}）",
                    frame_index=cache.frame_index_of(worst),
                    data={"std": round(float(stds[worst]), 2), "mean": round(float(means[worst]), 2)},
                )
            )

        notes = ""
        if bad_ratio > 0.05:
            notes = f"有 {int(bad.sum())}/{len(frames)} 帧属于黑屏或纯色"

        return CheckResult(
            check_id=self.id,
            score=score,
            passed=passed,
            detail=detail,
            evidence=evidence,
            notes=notes,
        )


register(BlackFrameCheck())
