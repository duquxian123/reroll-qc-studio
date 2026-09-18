"""闪烁检测（软性项）。

判据：整帧亮度序列出现高频抖动。用亮度序列的二阶差分衡量，
它对"逐渐变亮/变暗"这种正常变化不敏感，但对"忽明忽暗"很敏感。

已知局限：画面里出现真实的闪光（闪电、闪光灯）也会被判为闪烁。
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .base import CheckResult, Evidence, FrameCache, ramp
from .registry import register


class FlickerCheck:
    id = "flicker"
    name = "闪烁"
    description = "检测整帧亮度的高频抖动。AI 生成常见于光照不稳定、时序不一致。"
    unit = "分"
    hard = False
    default_weight = 1.0
    default_threshold = 75.0
    pass_when_above = True
    requires_models = False
    default_enabled = True

    def run(self, cache: FrameCache, params: dict[str, Any]) -> CheckResult:
        bad_energy = float(params.get("energy_bad", 9.0))
        good_energy = float(params.get("energy_good", 1.2))

        gray = cache.gray_frames()
        if len(gray) < 3:
            return CheckResult(
                check_id=self.id, score=100.0, passed=True,
                detail={"note": "帧数太少，跳过闪烁检测"},
            )

        brightness = gray.reshape(len(gray), -1).mean(axis=1)
        # 二阶差分：对单调变化不敏感，对来回抖动敏感
        second = np.diff(brightness, n=2)
        energy = float(np.abs(second).mean())
        peak = float(np.abs(second).max())
        swing = float(brightness.max() - brightness.min())

        score = ramp(energy, bad=bad_energy, good=good_energy)
        passed = score >= 75.0

        detail = {
            "sampled_frames": int(len(gray)),
            "flicker_energy": round(energy, 3),
            "flicker_peak": round(peak, 3),
            "brightness_swing": round(swing, 2),
            "energy_bad": bad_energy,
            "energy_good": good_energy,
        }

        evidence: list[Evidence] = [
            Evidence(
                kind="chart",
                label="逐帧亮度曲线",
                data={
                    "series": [round(float(v), 2) for v in brightness],
                    "second_diff": [round(float(v), 2) for v in second],
                },
            )
        ]
        if peak > bad_energy:
            worst = int(np.argmax(np.abs(second))) + 1
            evidence.append(
                Evidence(
                    kind="frame",
                    label=f"亮度跳变最大处（Δ²={peak:.1f}）",
                    frame_index=cache.frame_index_of(worst),
                    data={"delta2": round(peak, 2)},
                )
            )

        notes = ""
        if energy > good_energy:
            notes = f"亮度抖动能量 {energy:.2f}（越小越稳）"

        return CheckResult(
            check_id=self.id,
            score=score,
            passed=passed,
            detail=detail,
            evidence=evidence,
            notes=notes,
        )


register(FlickerCheck())
