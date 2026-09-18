"""噪点检测（软性项）。

判据：**只看画面平坦区域**的高频能量。

为什么不用简单的"中值滤波残差"
--------------------------------
中值滤波残差把"结构化细节"和"随机噪点"混在一起：细胞自动机（1 像素格子）、
分形图这类高细节素材会被误判成脏。而随机噪点的特征恰恰是
**在本来应该平坦的地方也出现高频抖动**，所以只在平坦区域测高频能量，
就能把两者分开。
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from .base import CheckResult, Evidence, FrameCache, ramp
from .registry import register

# 平坦区域占比：取梯度最小的这一部分像素作为"本该平坦"的区域
FLAT_PERCENTILE = 25
MIN_FLAT_PIXELS = 100


def _flat_noise(frame: np.ndarray) -> float:
    """平坦区域的平均高频能量。"""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
    lap = cv2.Laplacian(gray, cv2.CV_32F)
    grad = (
        np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0))
        + np.abs(cv2.Sobel(gray, cv2.CV_32F, 0, 1))
    )
    mask = grad <= np.percentile(grad, FLAT_PERCENTILE)
    if int(mask.sum()) < MIN_FLAT_PIXELS:
        return 0.0
    return float(np.abs(lap[mask]).mean())


def _noise_evidence(frame: np.ndarray, crop: int = 240) -> str | None:
    """做一张"原图 vs 残差放大"的并排对比图。

    噪点这个缺陷人眼很容易看不出来——放大一块平坦区域并把残差放大，
    人工才能复核算法判得对不对。
    """
    h, w = frame.shape[:2]
    size = min(crop, h, w)
    y0 = max(0, (h - size) // 2)
    x0 = max(0, (w - size) // 2)
    patch = frame[y0:y0 + size, x0:x0 + size]

    residual = cv2.absdiff(patch, cv2.medianBlur(patch, 3))
    residual = np.clip(residual.astype(np.int16) * 6, 0, 255).astype(np.uint8)

    scale = max(1, 300 // size)
    if scale > 1:
        patch = cv2.resize(patch, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
        residual = cv2.resize(residual, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)

    gap = np.full((patch.shape[0], 8, 3), 255, dtype=np.uint8)
    side = np.hstack([patch, gap, residual])

    cv2.putText(side, "original", (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)
    cv2.putText(side, "residual x6", (patch.shape[1] + 16, 20),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)

    ok, buf = cv2.imencode(".jpg", side, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        return None
    import base64

    return base64.b64encode(buf.tobytes()).decode("ascii")


class NoiseCheck:
    id = "noise"
    name = "噪点"
    description = "只在画面平坦区域测高频能量，能区分随机噪点和高细节内容。"
    unit = "分"
    hard = False
    default_weight = 0.8
    default_threshold = 70.0
    pass_when_above = True
    requires_models = False
    default_enabled = True

    def run(self, cache: FrameCache, params: dict[str, Any]) -> CheckResult:
        bad_level = float(params.get("level_bad", 4.0))
        good_level = float(params.get("level_good", 1.0))
        frames = cache.frames_subset(40)   # 噪点不需要逐帧，抽 40 帧足够稳定
        levels = np.array([_flat_noise(f) for f in frames], dtype=np.float32)
        level = float(np.median(levels))
        worst = int(np.argmax(levels))

        score = ramp(level, bad=bad_level, good=good_level)
        passed = score >= 70.0

        detail = {
            "sampled_frames": int(len(frames)),
            "noise_level": round(level, 3),
            "noise_max": round(float(levels.max()), 3),
            "level_bad": bad_level,
            "level_good": good_level,
            "flat_percentile": FLAT_PERCENTILE,
        }

        evidence: list[Evidence] = [
            Evidence(
                kind="chart",
                label="逐帧噪点强度（平坦区域）",
                data={"series": [round(float(v), 2) for v in levels]},
            )
        ]
        if level > good_level:
            b64 = _noise_evidence(frames[worst])
            evidence.append(
                Evidence(
                    kind="overlay",
                    label=f"噪点最重的一帧：原图 vs 残差放大（强度 {levels[worst]:.2f}）",
                    frame_index=cache.frame_index_of(worst),
                    data={"image_b64": b64} if b64 else {"noise": round(float(levels[worst]), 3)},
                )
            )

        notes = ""
        if score < 70.0:
            notes = f"平坦区域噪点强度 {level:.2f}（越低越干净）"

        return CheckResult(
            check_id=self.id,
            score=score,
            passed=passed,
            detail=detail,
            evidence=evidence,
            notes=notes,
        )


register(NoiseCheck())
