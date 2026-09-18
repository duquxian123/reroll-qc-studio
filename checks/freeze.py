"""卡帧 / 静帧检测（硬性项）。

判据：连续多帧之间几乎没有像素变化，说明画面卡住了。
注意已知误报：本身就是静止镜头的素材（比如定格画面）也会被判为卡帧，
所以这一项的阈值必须结合分镜意图来调。
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .base import CheckResult, Evidence, FrameCache, ramp
from .registry import register


class FreezeCheck:
    id = "freeze"
    name = "卡帧/静帧"
    description = "检测连续多帧完全重复的卡帧（重复帧检测，模糊不会误报）。硬性缺陷。"
    unit = "分"
    hard = True
    default_weight = 1.0
    default_threshold = 92.0
    pass_when_above = True
    requires_models = False
    default_enabled = True

    def run(self, cache: FrameCache, params: dict[str, Any]) -> CheckResult:
        freeze_thresh = float(params.get("frame_diff_max", 0.05))
        min_run = int(params.get("min_frozen_run", 3))

        gray = cache.gray_frames()
        if len(gray) < 2:
            return CheckResult(
                check_id=self.id, score=100.0, passed=True,
                detail={"note": "帧数太少，跳过卡帧检测"},
            )

        # 用**原始**帧差找"重复帧"：真卡帧是同一帧被克隆，差值几乎为 0；
        # 模糊虽然让差值变小，但绝不会变成 0。所以低阈值 + 连续段判定最稳。
        diffs = np.abs(np.diff(gray, axis=0)).reshape(len(gray) - 1, -1).mean(axis=1)
        frozen = diffs < freeze_thresh

        # 找出最长的连续冻结段
        longest, longest_start, run, start = 0, -1, 0, 0
        for i, flag in enumerate(frozen):
            if flag:
                if run == 0:
                    start = i
                run += 1
                if run > longest:
                    longest, longest_start = run, start
            else:
                run = 0

        frozen_ratio = float(frozen.mean())
        score = ramp(frozen_ratio, bad=0.40, good=0.0)
        passed = longest < min_run

        detail = {
            "sampled_frames": int(len(gray)),
            "frozen_pairs": int(frozen.sum()),
            "frozen_ratio": round(frozen_ratio, 4),
            "longest_frozen_run": int(longest),
            "mean_frame_diff": round(float(diffs.mean()), 3),
            "freeze_threshold": freeze_thresh,
        }

        evidence: list[Evidence] = []
        if longest >= min_run and longest_start >= 0:
            evidence.append(
                Evidence(
                    kind="frame",
                    label=f"最长卡帧段起点（连续 {longest} 帧几乎无变化）",
                    frame_index=cache.frame_index_of(longest_start),
                    data={"longest_run": int(longest)},
                )
            )

        notes = ""
        if longest >= min_run:
            notes = f"存在连续 {longest} 帧几乎无变化（阈值 {freeze_thresh}）"

        return CheckResult(
            check_id=self.id,
            score=score,
            passed=passed,
            detail=detail,
            evidence=evidence,
            notes=notes,
        )


register(FreezeCheck())
