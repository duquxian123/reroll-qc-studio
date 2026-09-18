"""规格检测：时长、分辨率、宽高比是否符合分镜要求。

这是硬性项——规格不对的素材根本不能用，直接一票否决。
"""

from __future__ import annotations

from typing import Any

from .base import CheckResult, FrameCache, clamp_score
from .registry import register


class SpecCheck:
    id = "spec"
    name = "规格"
    description = "时长、分辨率、宽高比是否符合分镜表的要求。规格不符的素材无法进入后期。"
    unit = "分"
    hard = True
    default_weight = 1.0
    default_threshold = 60.0
    pass_when_above = True
    requires_models = False
    default_enabled = True

    def run(self, cache: FrameCache, params: dict[str, Any]) -> CheckResult:
        meta = cache.meta

        expected_duration = params.get("expected_duration_s")
        duration_tol = float(params.get("duration_tolerance", 0.25))
        expected_aspect = params.get("expected_aspect")

        detail: dict[str, Any] = {
            "width": meta.width,
            "height": meta.height,
            "fps": round(meta.fps, 2),
            "duration_s": round(meta.duration_s, 2),
            "codec": meta.codec,
        }

        notes: list[str] = []
        passed = True

        # --- 时长 ---
        if expected_duration:
            expected_duration = float(expected_duration)
            rel_err = abs(meta.duration_s - expected_duration) / max(expected_duration, 1e-6)
            # 误差 0 → 100 分；误差达到 2 倍容差 → 0 分
            duration_score = clamp_score(100.0 * (1.0 - rel_err / (duration_tol * 2)))
            detail["expected_duration_s"] = expected_duration
            detail["duration_rel_error"] = round(rel_err, 4)
            if rel_err > duration_tol:
                passed = False
                notes.append(
                    f"时长 {meta.duration_s:.2f}s 偏离期望 {expected_duration:.2f}s "
                    f"超过 {duration_tol:.0%} 容差"
                )
        else:
            duration_score = 100.0

        # --- 宽高比 ---
        actual_aspect = meta.width / meta.height if meta.height else 0.0
        detail["aspect_ratio"] = round(actual_aspect, 4)
        if expected_aspect:
            exp = _parse_aspect(str(expected_aspect))
            detail["expected_aspect"] = exp
            if exp is None:
                aspect_score = 100.0
            elif abs(actual_aspect - exp) / exp <= 0.05:
                aspect_score = 100.0
            else:
                aspect_score = 0.0
                passed = False
                notes.append(f"宽高比 {actual_aspect:.2f} 与期望 {exp:.2f} 不符")
        else:
            aspect_score = 100.0

        # --- 分辨率下限 ---
        min_side = int(params.get("min_short_side", 0) or 0)
        if min_side:
            detail["min_short_side"] = min_side
            short_side = min(meta.width, meta.height)
            resolution_score = clamp_score(100.0 * short_side / min_side)
            if short_side < min_side:
                passed = False
                notes.append(f"短边 {short_side}px 低于要求 {min_side}px")
        else:
            resolution_score = 100.0

        score = min(duration_score, aspect_score, resolution_score)
        detail["duration_score"] = round(duration_score, 2)
        detail["aspect_score"] = round(aspect_score, 2)
        detail["resolution_score"] = round(resolution_score, 2)

        return CheckResult(
            check_id=self.id,
            score=score,
            passed=passed,
            detail=detail,
            notes="；".join(notes),
        )


def _parse_aspect(text: str) -> float | None:
    """把 '16:9' 解析成 1.7778；解析不了返回 None。"""
    text = text.strip()
    if not text:
        return None
    if ":" in text:
        left, _, right = text.partition(":")
        try:
            return float(left) / float(right)
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return float(text)
    except ValueError:
        return None


register(SpecCheck())
