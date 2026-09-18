"""会话状态持久化。

注意：这不是"给使用者看的报告"，界面上**不会出现任何保存/导出概念**。
它只是让"采纳 / 废弃"在刷新页面后不丢。使用者完全无感。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config


@dataclass
class SessionState:
    adopted: dict[str, str] = field(default_factory=dict)          # shot_id -> candidate_id
    discarded: dict[str, list[str]] = field(default_factory=dict)  # shot_id -> [candidate_id]
    reroll_count: dict[str, int] = field(default_factory=dict)
    last_config: list[dict] = field(default_factory=list)          # 上次的质检项配置
    last_pass_line: float = 75.0                                   # 上次的总分线
    last_soft_tolerance: float = 0.5                               # 上次的软性项容忍度
    project_id: str = "builtin"

    # -- 读写 -----------------------------------------------------------
    @classmethod
    def load(cls, path: Path | None = None) -> "SessionState":
        path = path or config.SESSION_PATH
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()
        return cls(
            adopted=dict(data.get("adopted", {})),
            discarded={k: list(v) for k, v in data.get("discarded", {}).items()},
            reroll_count=dict(data.get("reroll_count", {})),
            last_config=list(data.get("last_config", [])),
            last_pass_line=float(data.get("last_pass_line", 75.0)),
            last_soft_tolerance=float(data.get("last_soft_tolerance", 0.5)),
            project_id=data.get("project_id", "builtin"),
        )

    def save(self, path: Path | None = None) -> None:
        path = path or config.SESSION_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # -- 操作 -----------------------------------------------------------
    def adopt(self, shot_id: str, candidate_id: str) -> None:
        self.adopted[shot_id] = candidate_id
        # 采纳的同时把它从废弃列表里移除，避免状态自相矛盾
        discards = self.discarded.get(shot_id, [])
        if candidate_id in discards:
            discards.remove(candidate_id)
            self.discarded[shot_id] = discards

    def discard(self, shot_id: str, candidate_id: str) -> None:
        discards = self.discarded.setdefault(shot_id, [])
        if candidate_id not in discards:
            discards.append(candidate_id)
        if self.adopted.get(shot_id) == candidate_id:
            self.adopted.pop(shot_id, None)

    def clear_shot(self, shot_id: str) -> None:
        self.adopted.pop(shot_id, None)
        self.discarded.pop(shot_id, None)

    def bump_reroll(self, shot_id: str) -> int:
        self.reroll_count[shot_id] = self.reroll_count.get(shot_id, 0) + 1
        return self.reroll_count[shot_id]

    def reset(self) -> None:
        self.adopted.clear()
        self.discarded.clear()
        self.reroll_count.clear()
