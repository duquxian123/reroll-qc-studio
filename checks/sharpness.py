"""清晰度检测（软性项）。

判据：拉普拉斯方差。数值越高说明边缘越锐利，越低说明模糊或失焦。

阈值怎么定的（见 tools/calibrate.py 的实测数据）
------------------------------------------------
在合成测试集上实测：

    好片（含缩小过的真人素材）  74 – 41000
    模糊（轻/中/重）            0.7 – 6.8

分离度非常大，所以取 var_bad=10、var_good=80 就足够把两者分开。

已知局限
--------
拉普拉斯方差和画面内容强相关：大面积平坦背景（天空、纯色墙）即使清晰，
方差也会偏低。这是这一项最典型的误报来源，遇到这类镜头请把阈值调低。
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from .base import CheckResult, Evidence, FrameCache, ramp
from .registry import register


class SharpnessCheck:
    id = "sharpness"
    name = "清晰度"
    description = "拉普拉斯方差衡量画面锐利程度。数值越低越模糊。平坦背景会误报，需调阈值。"
    unit = "分"
    hard = False
    default_weight = 1.0
    default_threshold = 65.0
    pass_when_above = True
    requires_models = False
    default_enabled = True

    def run(self, cache: FrameCache, params: dict[str, Any]) -> CheckResult:
        bad_var = float(params.get("var_bad", 10.0))
        good_var = float(params.get("var_good", 80.0))

        gray = cache.gray_subset(40)       # 清晰度不需要逐帧
        variances = np.array(
            [float(cv2.Laplacian(f, cv2.CV_32F).var()) for f in gray], dtype=np.float32
        )
        # 用中位数而不是均值：个别清晰帧不该掩盖整体偏糊
        variance = float(np.median(variances))
        worst = int(np.argmin(variances))

        score = ramp(variance, bad=bad_var, good=good_var)
        passed = score >= 65.0

        detail = {
            "sampled_frames": int(len(gray)),
            "laplacian_variance": round(variance, 2),
            "variance_min": round(float(variances.min()), 2),
            "variance_max": round(float(variances.max()), 2),
            "var_bad": bad_var,
            "var_good": good_var,
        }

        evidence: list[Evidence] = [
            Evidence(
                kind="chart",
                label="逐帧清晰度（拉普拉斯方差）",
                data={"series": [round(float(v), 1) for v in variances]},
            ),
            Evidence(
                kind="frame",
                label=f"最模糊的一帧（方差 {variances[worst]:.1f}）",
                frame_index=cache.frame_index_of(worst),
                data={"variance": round(float(variances[worst]), 2)},
            ),
        ]

        notes = ""
        if score < 65.0:
            notes = f"拉普拉斯方差 {variance:.1f}（低于 {good_var:.0f} 视为偏糊）"

        return CheckResult(
            check_id=self.id,
            score=score,
            passed=passed,
            detail=detail,
            evidence=evidence,
            notes=notes,
        )


register(SharpnessCheck())
