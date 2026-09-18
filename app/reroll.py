"""重抽诊断：判断是「运气差」还是「提示词本身有问题」，并给出修改建议。

这是整套系统里最值钱的一环——它把「又崩了」变成
「因为提示词缺了对手部的约束」，这个结论人眼看十遍也未必总结得出来。

原则：**只给建议，不自动改**。提示词改写有越改越偏的风险，
而且没有 A/B 验证机制，自动改等于放大不确定性。人工确认后才重抽。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import CandidateResult, Shot

# 失败维度 → 提示词修正建议（规则库）
RULES: dict[str, dict[str, Any]] = {
    "sharpness": {
        "label": "模糊",
        "negative": ["模糊", "失焦", "低清晰度"],
        "positive": ["画面清晰", "高细节"],
        "param_hint": None,
    },
    "flicker": {
        "label": "闪烁",
        "negative": ["闪烁", "亮度跳变", "频闪"],
        "positive": ["光照稳定", "稳定曝光"],
        "param_hint": None,
    },
    "noise": {
        "label": "噪点",
        "negative": ["噪点", "颗粒", "画面脏"],
        "positive": ["画面干净"],
        "param_hint": None,
    },
    "hand": {
        "label": "手部崩坏",
        "negative": ["多余手指", "畸形手", "六指", "手指融合"],
        "positive": ["手部自然"],
        "param_hint": None,
    },
    "face": {
        "label": "人脸崩坏",
        "negative": ["扭曲的脸", "五官错位", "变形的脸"],
        "positive": ["五官清晰", "人脸自然"],
        "param_hint": None,
    },
    "freeze": {
        "label": "卡帧",
        "negative": ["静止", "卡顿"],
        "positive": ["运动流畅"],
        "param_hint": "如果分镜本身就是静止镜头，应调高该项阈值而不是改提示词。",
    },
    "blackframe": {
        "label": "黑屏/纯色",
        "negative": ["黑屏", "纯色画面"],
        "positive": [],
        "param_hint": "黑屏通常是生成中断，优先降低时长或简化画面内容。",
    },
    "spec": {
        "label": "规格不符",
        "negative": [],
        "positive": [],
        "param_hint": "这是生成参数问题（时长/分辨率），改提示词没用，请调整分镜参数。",
    },
}

# 集中度阈值：某个维度在这么多比例的候选上失败，就判定为"系统性缺陷"
SYSTEMATIC_RATIO = 0.75


@dataclass
class Diagnosis:
    kind: str                       # systematic / random
    dominant_check: str | None
    concentration: float
    message: str
    negative_additions: list[str] = field(default_factory=list)
    positive_additions: list[str] = field(default_factory=list)
    param_hint: str | None = None
    failure_distribution: dict[str, float] = field(default_factory=dict)
    suggested_prompt: str | None = None
    suggested_negative: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "dominant_check": self.dominant_check,
            "concentration": round(self.concentration, 3),
            "message": self.message,
            "negative_additions": self.negative_additions,
            "positive_additions": self.positive_additions,
            "param_hint": self.param_hint,
            "failure_distribution": {
                k: round(v, 3) for k, v in self.failure_distribution.items()
            },
            "suggested_prompt": self.suggested_prompt,
            "suggested_negative": self.suggested_negative,
        }


def diagnose(shot: Shot, results: list[CandidateResult]) -> Diagnosis | None:
    """对「全部候选都不通过」的分镜做诊断。"""
    if not results:
        return None

    total = len(results)
    counts: dict[str, int] = {}
    for r in results:
        # 只统计"没通过"的检测项
        failed_ids = {c.check_id for c in r.checks if not c.passed}
        failed_ids.update(r.hard_failed)
        for cid in failed_ids:
            counts[cid] = counts.get(cid, 0) + 1

    if not counts:
        return Diagnosis(
            kind="random",
            dominant_check=None,
            concentration=0.0,
            message="没有可归因的失败维度，建议直接重抽。",
        )

    distribution = {cid: n / total for cid, n in counts.items()}
    dominant, ratio = max(distribution.items(), key=lambda kv: kv[1])

    if ratio < SYSTEMATIC_RATIO:
        return Diagnosis(
            kind="random",
            dominant_check=dominant,
            concentration=ratio,
            message=(
                f"{total} 个候选的失败维度比较分散（最高为「{RULES.get(dominant, {}).get('label', dominant)}」"
                f"，占 {ratio:.0%}），更像是运气问题，建议直接重抽。"
            ),
            failure_distribution=distribution,
        )

    rule = RULES.get(dominant, {})
    label = rule.get("label", dominant)
    negative_additions = list(rule.get("negative", []))
    positive_additions = list(rule.get("positive", []))

    message = (
        f"{total} 个候选中有 {ratio:.0%} 都在「{label}」上失分——"
        f"这是系统性缺陷，不是运气问题。继续原样重抽大概率还是同样的结果，"
        f"建议先按下面的建议修改提示词。"
    )

    return Diagnosis(
        kind="systematic",
        dominant_check=dominant,
        concentration=ratio,
        message=message,
        negative_additions=negative_additions,
        positive_additions=positive_additions,
        param_hint=rule.get("param_hint"),
        failure_distribution=distribution,
        suggested_negative=_merge_negative(shot.negative_prompt, negative_additions),
        suggested_prompt=_append_positive(shot.prompt, positive_additions),
    )


def _merge_negative(existing: str, additions: list[str]) -> str | None:
    if not additions:
        return None
    parts = [p.strip() for p in (existing or "").replace("，", ",").split(",") if p.strip()]
    for item in additions:
        if item not in parts:
            parts.append(item)
    return ",".join(parts)


def _append_positive(prompt: str, additions: list[str]) -> str | None:
    if not additions:
        return None
    return f"{prompt}，{('，'.join(additions))}"
