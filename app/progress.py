"""质检进度（内存态，供前端轮询）。

质检一次全量要一两分钟，界面必须能告诉用户"跑到哪了"。
这里用最简单的进程内状态 + 轮询，不引入消息队列。
"""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()

_state: dict = {
    "running": False,
    "total": 0,
    "done": 0,
    "shot_id": "",
    "candidate_id": "",
    "started_at": 0.0,
    "elapsed": 0.0,
    "label": "",
}


def begin(total: int, label: str = "全部分镜") -> None:
    with _lock:
        _state.update(
            running=True,
            total=max(0, total),
            done=0,
            shot_id="",
            candidate_id="",
            started_at=time.time(),
            elapsed=0.0,
            label=label,
        )


def advance(shot_id: str, candidate_id: str) -> None:
    with _lock:
        _state["done"] += 1
        _state["shot_id"] = shot_id
        _state["candidate_id"] = candidate_id
        _state["elapsed"] = time.time() - _state["started_at"]


def end() -> None:
    with _lock:
        _state["running"] = False
        if _state["started_at"]:
            _state["elapsed"] = time.time() - _state["started_at"]
        _state["candidate_id"] = ""


def snapshot() -> dict:
    with _lock:
        data = dict(_state)
    total = data.get("total") or 0
    done = data.get("done") or 0
    data["percent"] = round(done / total * 100, 1) if total else 0.0
    data["elapsed"] = round(data.get("elapsed") or 0, 1)
    return data
