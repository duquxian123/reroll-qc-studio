"""项目状态的磁盘持久化：分镜表 + 抽到的候选。

为什么需要它
------------
质检结果可以通过重跑恢复，但"用户的分镜表 + 抽卡抽到的候选"是**用户数据**。
以前它只活在内存里，服务一重启就回到内置示例——等于改一行代码就要重新
导入、重新抽卡、重新质检。这个模块把它落到 `state/project.json`。

设计取舍
--------
* **只存分镜和候选，不存质检结果**：结果是派生数据，本来就被"候选指纹 /
  质检配置指纹"判定会失效；而且改完质检代码本来就该重跑。真要存结果，
  就得连带存证据图（base64，几十 MB）并处理指纹迁移，不划算。
* **不存 warnings**：那是导入时的一次性反馈（"忽略了无法识别的列"），
  存了会在每次重启时重复弹提示。
* **原子写**：先写 `.tmp` 再 `os.replace`，写到一半被杀不会留下半个坏文件。
* **版本号 + 全程容错**：认不出来的文件直接当作"没有"，绝不因为一个坏文件
  导致服务起不来。删掉这个文件就等于恢复出厂设置。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import config
from .dataset import Project
from .models import Shot

VERSION = 1


def save_project(project: Project | None, path: Path | None = None) -> None:
    """把当前项目写到磁盘。project 为 None（还没载入）时什么都不做。"""
    if project is None:
        return
    path = path or config.PROJECT_PATH

    payload = {
        "version": VERSION,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "project_id": project.project_id,
        "name": project.name,
        "source": project.source,
        "shots": [shot.to_dict() for shot in project.shots],
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, path)          # 原子替换：要么旧文件，要么新文件


def load_project(path: Path | None = None) -> Project | None:
    """读回上次的项目。读不出来（不存在 / 坏了 / 版本不认识）就返回 None。"""
    path = path or config.PROJECT_PATH
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict) or data.get("version") != VERSION:
        return None

    try:
        shots = [Shot.from_dict(item) for item in (data.get("shots") or [])]
    except (TypeError, ValueError, AttributeError):
        return None

    return Project(
        project_id=str(data.get("project_id") or "restored"),
        name=str(data.get("name") or "上次的项目"),
        shots=shots,
        source=str(data.get("source") or "restored"),
    )


def clear(path: Path | None = None) -> None:
    """删掉落盘的项目（恢复出厂设置用）。"""
    path = path or config.PROJECT_PATH
    path.unlink(missing_ok=True)
    path.with_suffix(path.suffix + ".tmp").unlink(missing_ok=True)
