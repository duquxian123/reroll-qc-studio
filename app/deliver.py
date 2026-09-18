"""交付：把采纳的片段归集出来，并合成一条整片预览。

为什么需要这一步
----------------
质检和采纳解决的是"**每个镜头**选哪条"，但一条片子不是 8 个独立镜头。
采纳完之后，真正要交出去的东西是：

    第一步 导出：每个已采纳的候选复制到 data/selected/，
                按分镜顺序改名 01_S001.mp4…，并写一份 selected.csv 清单
    第二步 预览：把这些片段拼成一条 preview.mp4，连起来看一遍

为什么两步都要"统一规格"
------------------------
不同候选来自不同生成批次，分辨率 / 帧率 / 编码可能不一致，剪辑软件和
concat 拼接都要求一致。所以导出时统一转码到**第一条采纳片段**的规格
（以它为基准，不无谓放大或缩小），预览片再用统一后的文件拼接。

预览片只负责"连起来看一眼"，不做转场、配乐、字幕——那些是剪辑软件的事。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .ffmpeg import VideoSpec, concat, normalize, probe

MANIFEST_NAME = "selected.csv"
PREVIEW_NAME = "preview.mp4"
CONCAT_LIST_NAME = "_concat.txt"


@dataclass
class SelectedClip:
    """一个"已采纳"的片段在交付里的位置。"""

    index: int
    shot_id: str
    scene: str
    prompt: str
    candidate_id: str
    source: Path
    filename: str
    total_score: float | None = None
    reroll_count: int = 0
    duration_s: float | None = None
    spec: VideoSpec | None = field(default=None, compare=False)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "shot_id": self.shot_id,
            "scene": self.scene,
            "prompt": self.prompt,
            "candidate_id": self.candidate_id,
            "filename": self.filename,
            "total_score": round(self.total_score, 1) if self.total_score is not None else None,
            "reroll_count": self.reroll_count,
            "duration_s": round(self.duration_s, 2) if self.duration_s else None,
        }


def collect(runtime) -> tuple[list[SelectedClip], list[str]]:
    """按项目顺序收集已采纳的片段。

    返回 (片段列表, 还没采纳的分镜号列表)。顺序 = 分镜表里的顺序，
    因为剪辑就是按这个顺序来的。
    """
    clips: list[SelectedClip] = []
    missing: list[str] = []

    for shot in runtime.require_project().shots:
        adopted_id = runtime.session.adopted.get(shot.shot_id)
        if not adopted_id:
            missing.append(shot.shot_id)
            continue

        candidate = next(
            (c for c in shot.candidates if c.candidate_id == adopted_id), None
        )
        if candidate is None:
            # 采纳记录指向的候选已经不在候选列表里（比如重抽过）
            missing.append(shot.shot_id)
            continue

        source = config.DATA_DIR / candidate.file
        if not source.exists():
            missing.append(shot.shot_id)
            continue

        score: float | None = None
        result = runtime.cached_result(shot)
        if result is not None:
            scored = next(
                (c for c in result.candidates if c.candidate_id == adopted_id), None
            )
            if scored is not None:
                score = scored.total_score

        clips.append(
            SelectedClip(
                index=len(clips) + 1,
                shot_id=shot.shot_id,
                scene=shot.scene or "",
                prompt=shot.prompt,
                candidate_id=candidate.candidate_id,
                source=source,
                filename=f"{len(clips) + 1:02d}_{shot.shot_id}.mp4",
                total_score=score,
                reroll_count=runtime.session.reroll_count.get(shot.shot_id, 0),
            )
        )
    return clips, missing


def export(clips: list[SelectedClip], out_dir: Path | None = None) -> dict:
    """把采纳的片段归集到 out_dir，统一规格并写清单。"""
    out_dir = out_dir or config.SELECTED_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    _clean(out_dir)

    base_spec = probe(clips[0].source)
    for clip in clips:
        clip.spec = probe(clip.source)
        dest = out_dir / clip.filename
        if not _fresh(dest, clip.source):
            normalize(clip.source, dest, base_spec, with_audio=clip.spec.has_audio)
        clip.duration_s = probe(dest).duration_s

    _write_manifest(clips, out_dir)
    return {
        "ok": True,
        "count": len(clips),
        "dir": str(out_dir),
        "spec": base_spec.label,
        "total_duration_s": round(sum(c.duration_s or 0 for c in clips), 2),
        "files": [c.filename for c in clips],
        "clips": [c.to_dict() for c in clips],
        "manifest": str(out_dir / MANIFEST_NAME),
    }


def preview(clips: list[SelectedClip], out_dir: Path | None = None) -> dict:
    """先归集（保证规格统一），再拼成一条预览片。"""
    out_dir = out_dir or config.SELECTED_DIR
    result = export(clips, out_dir)

    files = [out_dir / c.filename for c in clips]
    base_spec = probe(files[0])
    out = out_dir / PREVIEW_NAME
    concat(files, out, base_spec, out_dir / CONCAT_LIST_NAME)
    (out_dir / CONCAT_LIST_NAME).unlink(missing_ok=True)

    spec = probe(out)
    result.update(
        {
            "preview": str(out),
            "preview_name": PREVIEW_NAME,
            "preview_duration_s": round(spec.duration_s, 2),
            "preview_spec": spec.label,
            "preview_mb": round(out.stat().st_size / 1e6, 1),
        }
    )
    return result


# ---------------------------------------------------------------------------


def _clean(out_dir: Path) -> None:
    """清掉上一次的产物（这个目录完全是生成物）。"""
    for pattern in ("*.mp4", MANIFEST_NAME, CONCAT_LIST_NAME):
        for path in out_dir.glob(pattern):
            path.unlink(missing_ok=True)


def _fresh(dest: Path, src: Path) -> bool:
    """目标已存在且不比源文件旧，就不用重新转码。"""
    if not dest.exists():
        return False
    try:
        return dest.stat().st_mtime >= src.stat().st_mtime
    except OSError:
        return False


def _write_manifest(clips: list[SelectedClip], out_dir: Path) -> None:
    """给剪辑/后期看的清单：哪一条对应哪个分镜、得分多少。"""
    header = [
        "顺序", "分镜号", "场景", "提示词", "采纳候选",
        "质检总分", "抽卡轮次", "文件名", "时长(秒)",
    ]
    with (out_dir / MANIFEST_NAME).open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for clip in clips:
            writer.writerow(
                [
                    clip.index,
                    clip.shot_id,
                    clip.scene,
                    clip.prompt,
                    clip.candidate_id,
                    "" if clip.total_score is None else f"{clip.total_score:.1f}",
                    clip.reroll_count,
                    clip.filename,
                    "" if clip.duration_s is None else f"{clip.duration_s:.2f}",
                ]
            )
