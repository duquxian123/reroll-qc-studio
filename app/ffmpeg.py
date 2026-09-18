"""ffmpeg / ffprobe 的定位与调用。

原来"找 ffmpeg"这件事在三个地方各写了一遍（合成测试集、mock 兜底、交付导出）。
集中到这里：先看 PATH，再翻 WinGet 的安装目录。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Windows 上 winget 装的 ffmpeg 不在 PATH 里，去这些目录翻
_SEARCH_ROOTS = (
    Path.home() / "AppData/Local/Microsoft/WinGet/Packages",
)


def _locate(name: str) -> str:
    exe = shutil.which(name)
    if exe:
        return exe
    for root in _SEARCH_ROOTS:
        if root.exists():
            for path in root.rglob(f"{name}.exe"):
                return str(path)
    raise RuntimeError(
        f"找不到 {name}，请先安装 ffmpeg（winget install Gyan.FFmpeg）"
    )


def ffmpeg() -> str:
    return _locate("ffmpeg")


def ffprobe() -> str:
    return _locate("ffprobe")


def run(cmd: list[str], what: str = "ffmpeg") -> None:
    """跑一条命令，失败就把 stderr 前 600 字抛出来。"""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{what} 失败：{result.stderr.strip()[:600]}")


@dataclass(frozen=True)
class VideoSpec:
    """一段视频的规格。交付时要"统一规格"，基准就是它。"""

    width: int = 854
    height: int = 480
    fps: float = 25.0
    duration_s: float = 0.0
    has_audio: bool = False

    @property
    def label(self) -> str:
        return f"{self.width}x{self.height} @ {self.fps:g}fps"


def probe(path: Path) -> VideoSpec:
    """读一段视频的分辨率 / 帧率 / 时长 / 有没有音轨。"""
    cmd = [
        ffprobe(), "-v", "error",
        "-show_entries", "stream=codec_type,width,height,r_frame_rate:format=duration",
        "-of", "json", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 读不了 {path.name}：{result.stderr.strip()[:400]}")

    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        data = {}

    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    fps = 25.0
    rate = video.get("r_frame_rate") or ""
    if "/" in rate:
        num, den = rate.split("/", 1)
        try:
            fps = float(num) / float(den or 1)
        except (ValueError, ZeroDivisionError):
            fps = 25.0

    duration = (data.get("format") or {}).get("duration")
    return VideoSpec(
        width=int(video.get("width") or 854),
        height=int(video.get("height") or 480),
        fps=round(fps, 3) if fps else 25.0,
        duration_s=float(duration) if duration else 0.0,
        has_audio=has_audio,
    )


def normalize(src: Path, dst: Path, spec: VideoSpec, with_audio: bool | None = None) -> None:
    """把片段转成统一规格。

    等比缩放后补黑边（不裁剪，避免把画面里的人脸切掉），再统一帧率、
    像素格式和编码。这样剪辑软件和 concat 拼接都不会因为参数不一致出错。
    """
    keep_audio = src_has_audio(src) if with_audio is None else with_audio
    filters = (
        f"scale={spec.width}:{spec.height}:force_original_aspect_ratio=decrease,"
        f"pad={spec.width}:{spec.height}:(ow-iw)/2:(oh-ih)/2:black,"
        f"setsar=1,format=yuv420p"
    )
    cmd = [
        ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-vf", filters,
        "-r", f"{spec.fps:g}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
    ]
    cmd += ["-c:a", "aac", "-b:a", "128k"] if keep_audio else ["-an"]
    cmd += ["-movflags", "+faststart", str(dst)]
    run(cmd, f"统一规格 {src.name}")


def src_has_audio(path: Path) -> bool:
    try:
        return probe(path).has_audio
    except RuntimeError:
        return False


def concat(files: list[Path], out: Path, spec: VideoSpec, list_path: Path) -> None:
    """按顺序把若干统一规格的片段拼成一条。"""
    list_path.write_text(
        "\n".join(f"file '{f.name}'" for f in files), encoding="utf-8"
    )
    keep_audio = all(src_has_audio(f) for f in files) if files else False

    cmd = [
        ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", f"{spec.fps:g}",
    ]
    cmd += ["-c:a", "aac", "-b:a", "128k"] if keep_audio else ["-an"]
    cmd += ["-movflags", "+faststart", str(out)]
    run(cmd, "拼接预览片")
