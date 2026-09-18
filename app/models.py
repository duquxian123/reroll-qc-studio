"""领域模型：分镜、候选、质检配置、质检结果、判定。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from checks.base import CheckResult


@dataclass
class Candidate:
    """一个候选视频。"""

    candidate_id: str
    file: str                       # 相对 data/samples 的路径
    shot_id: str
    defects: list[str] = field(default_factory=list)   # ground truth（合成集才有）
    severity: str = "none"
    expected_verdict: str | None = None                # accept / reject
    note: str = ""
    is_spare: bool = False
    provider: str = "mock"
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "file": self.file,
            "shot_id": self.shot_id,
            "defects": self.defects,
            "severity": self.severity,
            "expected_verdict": self.expected_verdict,
            "note": self.note,
            "is_spare": self.is_spare,
            "provider": self.provider,
            "params": self.params,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Candidate":
        """从磁盘恢复（和 to_dict 配对，用于状态落盘）。"""
        return cls(
            candidate_id=str(data.get("candidate_id") or ""),
            file=str(data.get("file") or ""),
            shot_id=str(data.get("shot_id") or ""),
            defects=[str(d) for d in (data.get("defects") or [])],
            severity=str(data.get("severity") or "none"),
            expected_verdict=data.get("expected_verdict"),
            note=str(data.get("note") or ""),
            is_spare=bool(data.get("is_spare", False)),
            provider=str(data.get("provider") or "mock"),
            params=dict(data.get("params") or {}),
        )


@dataclass
class Shot:
    """一个分镜。"""

    shot_id: str
    scene: str
    prompt: str
    expected_faces: int | None = None
    expected_hands: int | None = None
    expected_duration_s: float | None = None
    aspect_ratio: str | None = None
    negative_prompt: str = ""
    ref_image: str | None = None
    priority: str = "medium"
    candidates_per_shot: int | None = None    # 抽卡时抽几条候选，None = 用默认值
    notes: str = ""
    candidates: list[Candidate] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "shot_id": self.shot_id,
            "scene": self.scene,
            "prompt": self.prompt,
            "expected_faces": self.expected_faces,
            "expected_hands": self.expected_hands,
            "expected_duration_s": self.expected_duration_s,
            "aspect_ratio": self.aspect_ratio,
            "negative_prompt": self.negative_prompt,
            "ref_image": self.ref_image,
            "priority": self.priority,
            "candidates_per_shot": self.candidates_per_shot,
            "notes": self.notes,
            "candidates": [c.to_dict() for c in self.candidates],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Shot":
        """从磁盘恢复（和 to_dict 配对，用于状态落盘）。"""
        return cls(
            shot_id=str(data.get("shot_id") or ""),
            scene=str(data.get("scene") or ""),
            prompt=str(data.get("prompt") or ""),
            expected_faces=_optional_int(data.get("expected_faces")),
            expected_hands=_optional_int(data.get("expected_hands")),
            expected_duration_s=_optional_float(data.get("expected_duration_s")),
            aspect_ratio=data.get("aspect_ratio"),
            negative_prompt=str(data.get("negative_prompt") or ""),
            ref_image=data.get("ref_image"),
            priority=str(data.get("priority") or "medium"),
            candidates_per_shot=_optional_int(data.get("candidates_per_shot")),
            notes=str(data.get("notes") or ""),
            candidates=[
                Candidate.from_dict(c) for c in (data.get("candidates") or [])
            ],
        )


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass
class CheckConfig:
    """界面上对某一项质检的配置。"""

    check_id: str
    enabled: bool = True
    order: int = 0
    threshold: float | None = None
    weight: float | None = None
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "enabled": self.enabled,
            "order": self.order,
            "threshold": self.threshold,
            "weight": self.weight,
            "params": self.params,
        }


@dataclass
class CandidateResult:
    """一个候选的完整质检结果。"""

    candidate_id: str
    shot_id: str
    file: str
    checks: list[CheckResult] = field(default_factory=list)
    total_score: float = 0.0
    passed: bool = False
    hard_failed: list[str] = field(default_factory=list)
    soft_failed: list[str] = field(default_factory=list)
    penalty_ratio: float = 0.0
    rank: int | None = None
    expected_verdict: str | None = None
    defects: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self, include_evidence: bool = True) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "shot_id": self.shot_id,
            "file": self.file,
            "total_score": round(self.total_score, 1),
            "passed": self.passed,
            "hard_failed": self.hard_failed,
            "soft_failed": self.soft_failed,
            "penalty_ratio": round(self.penalty_ratio, 4),
            "rank": self.rank,
            "expected_verdict": self.expected_verdict,
            "defects": self.defects,
            "error": self.error,
            "checks": [
                c.to_dict() if include_evidence else _brief(c) for c in self.checks
            ],
        }


def _brief(c: CheckResult) -> dict[str, Any]:
    return {
        "check_id": c.check_id,
        "score": round(c.score, 2),
        "passed": c.passed,
        "detail": c.detail,
        "notes": c.notes,
        "evidence": [],
    }


@dataclass
class ShotResult:
    """一个分镜的质检汇总。"""

    shot: Shot
    candidates: list[CandidateResult] = field(default_factory=list)
    status: str = "review"          # review / accepted / needs_reroll
    adopted_candidate_id: str | None = None
    diagnosis: dict[str, Any] | None = None
    reroll_count: int = 0

    @property
    def passed_candidates(self) -> list[CandidateResult]:
        return [c for c in self.candidates if c.passed]

    def to_dict(self, include_evidence: bool = True) -> dict[str, Any]:
        return {
            "shot": self.shot.to_dict(),
            "status": self.status,
            "adopted_candidate_id": self.adopted_candidate_id,
            "diagnosis": self.diagnosis,
            "reroll_count": self.reroll_count,
            "candidates": [c.to_dict(include_evidence) for c in self.candidates],
        }


@dataclass
class RunSummary:
    """一次质检运行的统计，用于界面上"命中/漏检/误杀"的实时读数。"""

    shots: int = 0
    candidates: int = 0
    passed: int = 0
    failed: int = 0
    labeled: int = 0        # 有 ground truth 的候选数
    true_pass: int = 0      # 判通过且标注为 accept
    false_reject: int = 0   # 判不通过但标注为 accept（误杀）
    true_reject: int = 0    # 判不通过且标注为 reject
    false_accept: int = 0   # 判通过但标注为 reject（漏检）
    needs_reroll_shots: int = 0

    @property
    def false_reject_rate(self) -> float | None:
        denom = self.true_pass + self.false_reject
        return self.false_reject / denom if denom else None

    @property
    def false_accept_rate(self) -> float | None:
        denom = self.true_reject + self.false_accept
        return self.false_accept / denom if denom else None

    def to_dict(self) -> dict[str, Any]:
        def pct(v: float | None) -> float | None:
            return None if v is None else round(v * 100, 1)

        return {
            "shots": self.shots,
            "candidates": self.candidates,
            "passed": self.passed,
            "failed": self.failed,
            "labeled": self.labeled,
            "true_pass": self.true_pass,
            "false_reject": self.false_reject,
            "true_reject": self.true_reject,
            "false_accept": self.false_accept,
            "false_reject_rate": pct(self.false_reject_rate),
            "false_accept_rate": pct(self.false_accept_rate),
            "needs_reroll_shots": self.needs_reroll_shots,
        }
