"""运行态：当前项目、质检结果缓存、以及**每个分镜的状态**。

为什么单独抽出来
----------------
"这个分镜现在处于什么状态"是整套系统的核心问题：

    empty        未生成   —— 还没有候选视频
    pending      待质检   —— 有候选，但还没跑过质检（或候选变了）
    review       待审     —— 质检完成，有通过的候选，等人工采纳
    needs_reroll 需重抽   —— 质检完成，但没有一条通过
    accepted     已采纳   —— 已经选定了某一条

之前这个判断散落在前端和各个接口里各写一遍，于是出现两个 bug：
  * 没质检的分镜也被"一键采纳第一名"采纳了
  * 已经质检过的分镜会被重复质检

现在把它收敛成**唯一的判定入口**：任何地方要问"这分镜什么状态"，
都只能问这里。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from checks import all_checks

from .dataset import Project
from .models import CheckConfig, Shot, ShotResult
from .project_store import save_project
from .store import SessionState

# 状态常量
EMPTY = "empty"
PENDING = "pending"
REVIEW = "review"
NEEDS_REROLL = "needs_reroll"
ACCEPTED = "accepted"

STATUS_LABELS = {
    EMPTY: "未生成",
    PENDING: "待质检",
    REVIEW: "待审",
    NEEDS_REROLL: "需重抽",
    ACCEPTED: "已采纳",
}


def parse_configs(
    configs: list[dict[str, Any]] | list[CheckConfig],
) -> list[CheckConfig]:
    """把界面传来的配置规范化成"实际要跑的检测项列表"。

    * **完全没传配置**（比如直接调 API）→ 回退到全部检测项
    * **传了配置但一项都没勾选** → 返回空列表，表示"什么都不跑"

    这两种情况必须区分开。以前一律回退成"全部"，导致用户在界面上
    把检测项全部取消后点质检，反而把所有检测项都跑了一遍。
    """
    if not configs:
        return [
            CheckConfig(check_id=c.id, order=i) for i, c in enumerate(all_checks())
        ]
    parsed = [c if isinstance(c, CheckConfig) else CheckConfig(**c) for c in configs]
    active = [c for c in parsed if c.enabled]
    active.sort(key=lambda c: c.order)
    return active


def config_signature(
    active: list[CheckConfig], pass_line: float, soft_tolerance: float
) -> str:
    """质检配置的指纹。

    改了检测项、阈值、权重、总分线或容忍度，指纹就变——
    这样"改了配置但没重跑"的分镜会自动回到「待质检」，
    而不是拿着旧配置的结果冒充新配置的结果。
    """
    parts = [
        f"{c.check_id}|{c.threshold}|{c.weight}|"
        f"{json.dumps(c.params, sort_keys=True, ensure_ascii=False)}"
        for c in active
    ]
    return f"{';'.join(parts)}#pass={pass_line}#tol={soft_tolerance}"


@dataclass
class CachedResult:
    """一次质检结果 + 它对应的"候选集合指纹"和"配置指纹"。"""

    signature: str
    config_sig: str
    result: ShotResult
    checked_at: float = field(default_factory=time.time)


class Runtime:
    def __init__(self, session: SessionState | None = None) -> None:
        self.project: Project | None = None
        self.results: dict[str, CachedResult] = {}
        self.session = session or SessionState.load()

    # -- 项目 -----------------------------------------------------------
    def set_project(self, project: Project, reset_results: bool = True) -> None:
        self.project = project
        if reset_results:
            self.results.clear()

    def require_project(self) -> Project:
        if self.project is None:
            raise RuntimeError("还没有载入项目")
        return self.project

    # -- 落盘 -----------------------------------------------------------
    def save(self) -> None:
        """把"会跨重启保留的东西"一次写完。

        * 会话状态（采纳记录 / 重抽次数 / 质检配置）→ state/session.json
        * 项目（分镜表 + 抽到的候选）            → state/project.json

        质检结果**不落盘**：它是派生数据，本来就会被指纹判定失效，
        而且改完质检代码本来就该重跑。
        """
        self.session.save()
        save_project(self.project)

    # -- 候选指纹：候选集合变了，之前的质检结果就失效 -------------------
    @staticmethod
    def signature(shot: Shot) -> str:
        return "|".join(c.candidate_id for c in shot.candidates)

    def current_config_signature(self) -> str:
        """当前（上一次运行的）质检配置指纹。"""
        active = parse_configs(self.session.last_config)
        return config_signature(
            active, self.session.last_pass_line, self.session.last_soft_tolerance
        )

    def cached_result(self, shot: Shot) -> ShotResult | None:
        """候选集合或质检配置变过，缓存就失效。"""
        cached = self.results.get(shot.shot_id)
        if cached is None:
            return None
        if cached.signature != self.signature(shot):
            return None
        if cached.config_sig != self.current_config_signature():
            return None
        return cached.result

    def store(self, shot: Shot, result: ShotResult) -> None:
        self.results[shot.shot_id] = CachedResult(
            signature=self.signature(shot),
            config_sig=self.current_config_signature(),
            result=result,
        )

    # -- 状态判定（唯一入口） -------------------------------------------
    def status(self, shot: Shot) -> str:
        """优先级：未生成 > 待质检 > 需重抽 > 已采纳 > 待审。

        注意"待质检"排在"已采纳"前面：只要候选或质检配置变过，
        旧结果就不再代表当前配置，这时应该提示重新质检，
        而不是拿旧结论冒充新配置的结论。
        """
        if not shot.candidates:
            return EMPTY
        result = self.cached_result(shot)
        if result is None:
            return PENDING
        if result.status == NEEDS_REROLL:
            return NEEDS_REROLL
        if self.session.adopted.get(shot.shot_id):
            return ACCEPTED
        return REVIEW

    def status_of(self, shot_id: str) -> str:
        shot = self.find(shot_id)
        return self.status(shot) if shot else EMPTY

    def find(self, shot_id: str) -> Shot | None:
        for shot in self.require_project().shots:
            if shot.shot_id == shot_id:
                return shot
        return None

    # -- 待质检集合 -----------------------------------------------------
    def pending_shots(
        self, shot_ids: list[str] | None = None, force: bool = False
    ) -> list[Shot]:
        """需要（重新）质检的分镜。

        force=True 时忽略缓存，全部重跑。
        否则只返回"没质检过"或"候选变了"的分镜。
        """
        wanted = set(shot_ids) if shot_ids else None
        out: list[Shot] = []
        for shot in self.require_project().shots:
            if wanted and shot.shot_id not in wanted:
                continue
            if not shot.candidates:
                continue
            if force or self.cached_result(shot) is None:
                out.append(shot)
        return out

    # -- 视图 -----------------------------------------------------------
    def project_view(self) -> dict[str, Any]:
        project = self.require_project()
        data = project.to_dict()
        for shot_dict in data["shots"]:
            shot_dict["status"] = self.status_of(shot_dict["shot_id"])
        data["status_summary"] = self.status_summary()
        return data

    def status_summary(self) -> dict[str, int]:
        counts = {key: 0 for key in STATUS_LABELS}
        for shot in self.require_project().shots:
            counts[self.status(shot)] += 1
        return counts

    def summary_view(self) -> dict[str, Any]:
        return {
            "counts": self.status_summary(),
            "labels": STATUS_LABELS,
            "checked_shots": sum(
                1 for s in self.require_project().shots if self.cached_result(s)
            ),
            "total_shots": len(self.require_project().shots),
        }

    def all_results(self) -> list[ShotResult]:
        """所有仍然有效的质检结果（候选变过的会被跳过）。"""
        out: list[ShotResult] = []
        for shot in self.require_project().shots:
            result = self.cached_result(shot)
            if result is not None:
                out.append(result)
        return out

    def session_view(self) -> dict[str, Any]:
        return {
            "adopted": self.session.adopted,
            "reroll_count": self.session.reroll_count,
        }
