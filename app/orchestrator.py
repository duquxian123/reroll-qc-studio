"""质检编排：跑检测项 → 判定 → 排名 → 诊断。

判定规则（与规格一致）
----------------------
* 每项输出 0-100 分
* **硬性项**（规格 / 黑屏 / 卡帧）任一不过 → 直接不通过，不参与排名
* 软性项不达标的权重之和超过容忍度 → 不通过
* 总分 = 加权平均分 × (1 - 未达标项的权重占比)，用于排名
* 通过的候选按总分降序排名
"""

from __future__ import annotations

import traceback
from typing import Any

from checks import FrameCache, get_check

from . import config, progress
from .models import (
    Candidate,
    CandidateResult,
    CheckConfig,
    RunSummary,
    Shot,
    ShotResult,
)
from .reroll import diagnose
from .runtime import Runtime, parse_configs

# 总分线：低于它判不通过。界面上可调。
DEFAULT_PASS_LINE = 75.0

# 软性项容忍度：不达标的软性项权重之和超过它，就判不通过。
# 默认 0.5 意味着"权重高于 0.5 的软性项一旦不达标就判废"，
# 而权重 0.5 的人脸/手部（未校准）单独失分可以被容忍。
DEFAULT_SOFT_TOLERANCE = 0.5


def summarize(results: list[ShotResult]) -> RunSummary:
    """汇总统计。误杀/漏检是拿内置测试集的缺陷标签算的，不是人工复核结果。"""
    summary = RunSummary(shots=len(results))
    for shot_result in results:
        if shot_result.status == "needs_reroll":
            summary.needs_reroll_shots += 1

        for c in shot_result.candidates:
            summary.candidates += 1
            if c.passed:
                summary.passed += 1
            else:
                summary.failed += 1

            if c.expected_verdict is None:
                continue
            summary.labeled += 1
            truth = c.expected_verdict
            if c.passed and truth == "accept":
                summary.true_pass += 1
            elif not c.passed and truth == "accept":
                summary.false_reject += 1
            elif not c.passed and truth == "reject":
                summary.true_reject += 1
            elif c.passed and truth == "reject":
                summary.false_accept += 1
    return summary


class Orchestrator:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.project = runtime.require_project()
        self.session = runtime.session
        # 候选的 file 是相对 data/ 的路径（如 pool/clip_07_noise-heavy.mp4）
        self.root = config.DATA_DIR

    # -- 对外入口 -------------------------------------------------------
    def run_shots(
        self,
        shots: list[Shot],
        configs: list[dict[str, Any]] | list[CheckConfig],
        pass_line: float = DEFAULT_PASS_LINE,
        soft_tolerance: float = DEFAULT_SOFT_TOLERANCE,
    ) -> list[ShotResult]:
        """质检指定的分镜。调用方负责决定"哪些分镜需要质检"。"""
        active = parse_configs(configs)
        total = sum(len(s.candidates) for s in shots)
        progress.begin(total, f"{len(shots)} 个分镜")
        try:
            results = [
                self._run_shot(shot, active, pass_line, soft_tolerance)
                for shot in shots
            ]
        finally:
            progress.end()

        # 结果写回运行态缓存（带候选指纹）
        for shot, result in zip(shots, results):
            self.runtime.store(shot, result)
        return results

    def run_one(
        self,
        shot: Shot,
        configs: list[dict[str, Any]] | list[CheckConfig],
        pass_line: float = DEFAULT_PASS_LINE,
        soft_tolerance: float = DEFAULT_SOFT_TOLERANCE,
    ) -> ShotResult:
        active = parse_configs(configs)
        progress.begin(len(shot.candidates), shot.shot_id)
        try:
            result = self._run_shot(shot, active, pass_line, soft_tolerance)
        finally:
            progress.end()
        self.runtime.store(shot, result)
        return result

    # -- 单个分镜 -------------------------------------------------------
    def _run_shot(
        self, shot: Shot, configs: list[CheckConfig], pass_line: float, soft_tolerance: float
    ) -> ShotResult:
        shot_result = ShotResult(
            shot=shot, reroll_count=self.session.reroll_count.get(shot.shot_id, 0)
        )

        for candidate in shot.candidates:
            progress.advance(shot.shot_id, candidate.candidate_id)
            shot_result.candidates.append(
                self._run_candidate(shot, candidate, configs, pass_line, soft_tolerance)
            )

        # --- 排名：只有通过的候选才参与 ---
        passed = [c for c in shot_result.candidates if c.passed and not c.error]
        passed.sort(key=lambda c: c.total_score, reverse=True)
        for rank, cand in enumerate(passed, start=1):
            cand.rank = rank

        # --- 分镜状态（采纳状态由 Runtime 统一判定，这里只判质检结论）---
        if not passed:
            shot_result.status = "needs_reroll"
            diagnosis = diagnose(shot, shot_result.candidates)
            shot_result.diagnosis = diagnosis.to_dict() if diagnosis else None
        else:
            shot_result.status = "review"

        adopted = self.session.adopted.get(shot.shot_id)
        if adopted and any(c.candidate_id == adopted for c in shot_result.candidates):
            shot_result.adopted_candidate_id = adopted

        return shot_result

    # -- 单个候选 -------------------------------------------------------
    def _run_candidate(
        self,
        shot: Shot,
        candidate: Candidate,
        configs: list[CheckConfig],
        pass_line: float,
        soft_tolerance: float,
    ) -> CandidateResult:
        result = CandidateResult(
            candidate_id=candidate.candidate_id,
            shot_id=shot.shot_id,
            file=candidate.file,
            expected_verdict=candidate.expected_verdict,
            defects=candidate.defects,
        )

        path = self.root / candidate.file
        if not path.exists():
            result.error = f"文件不存在：{candidate.file}"
            result.passed = False
            return result

        try:
            cache = FrameCache(path)
        except Exception as exc:  # noqa: BLE001
            result.error = f"无法读取视频：{exc}"
            result.passed = False
            return result

        weighted_sum = 0.0
        weight_total = 0.0
        soft_failed_weight = 0.0

        for cfg in configs:
            check = get_check(cfg.check_id)
            params = self._merge_params(shot, cfg)

            try:
                check_result = check.run(cache, params)
            except Exception as exc:  # noqa: BLE001 - 单项失败不该拖垮整次质检
                from checks.base import CheckResult as CR

                check_result = CR(
                    check_id=check.id,
                    score=0.0,
                    passed=False,
                    detail={"error": str(exc)},
                    notes=f"检测项执行失败：{exc}",
                )
                traceback.print_exc()

            threshold = (
                cfg.threshold if cfg.threshold is not None else check.default_threshold
            )
            check_result.passed = _apply_threshold(
                check_result.score, threshold, check.pass_when_above
            )
            check_result.detail["threshold"] = threshold

            weight = cfg.weight if cfg.weight is not None else check.default_weight
            check_result.detail["weight"] = weight

            result.checks.append(check_result)
            weighted_sum += check_result.score * weight
            weight_total += weight

            if not check_result.passed:
                if check.hard:
                    result.hard_failed.append(check.id)
                else:
                    result.soft_failed.append(check.id)
                    soft_failed_weight += weight

        # 总分 = 加权平均分 × (1 - 未达标项的权重占比)，仅用于排名
        weighted_avg = weighted_sum / weight_total if weight_total else 0.0
        result.penalty_ratio = soft_failed_weight / weight_total if weight_total else 0.0
        result.total_score = weighted_avg * (1.0 - result.penalty_ratio)

        # 判定：硬性项一票否决；软性项不达标的权重之和超过容忍度也判废。
        result.passed = (
            not result.hard_failed
            and soft_failed_weight <= soft_tolerance
            and result.total_score >= pass_line
        )
        return result

    # -- 参数合并 -------------------------------------------------------
    @staticmethod
    def _merge_params(shot: Shot, cfg: CheckConfig) -> dict[str, Any]:
        params: dict[str, Any] = {
            "expected_duration_s": shot.expected_duration_s,
            "expected_aspect": shot.aspect_ratio,
            "expected_faces": shot.expected_faces,
            "expected_hands": shot.expected_hands,
        }
        params.update(cfg.params)
        return params


def _apply_threshold(score: float, threshold: float, pass_when_above: bool) -> bool:
    if pass_when_above:
        return score >= threshold
    return score <= threshold
