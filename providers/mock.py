"""Mock Provider：从资源池里"抽卡"。

为什么这样设计
--------------
真实场景下，"生成"是调用云端 API，每次拿回来的片段都不一样、质量也不一样。
demo 里没有 API key，就用一个**本地资源池**来模拟这件事：

    每次 submit() 从池子里随机抽一个片段 → 就相当于"云端返回了一条生成结果"

池子里的文件名自带状态（`clip_07_noise-heavy.mp4`），所以抽完能立刻知道
"这次抽到的是好片还是废片"，方便核对质检判得对不对。

它和真 Provider 走完全相同的接口，以后换成 Seedance 不需要改编排逻辑。
"""

from __future__ import annotations

import random
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .base import Capabilities, GenJobRef, GenRequest

# 池子为空时的兜底：本地现场合成（保证界面永远有东西可抽）
WIDTH, HEIGHT, FPS = 854, 480, 25
BASE_SOURCES = {
    "mandelbrot": f"mandelbrot=size={WIDTH}x{HEIGHT}:rate={FPS}",
    "gradients": f"gradients=size={WIDTH}x{HEIGHT}:rate={FPS}:speed=0.06",
    "testsrc2": f"testsrc2=size={WIDTH}x{HEIGHT}:rate={FPS}",
    "life": f"life=size={WIDTH}x{HEIGHT}:rate={FPS}:mold=10:ratio=0.12:death_color=0x102030",
}
BASE_HUE = {"mandelbrot": 0, "gradients": 30, "testsrc2": 200, "life": 120}

BLUR_SIGMA = {"light": 2.4, "medium": 4.5, "heavy": 8.0}
NOISE_STRENGTH = {"light": 22, "medium": 38, "heavy": 60}
FLICKER_AMP = {"light": 0.10, "medium": 0.18, "heavy": 0.26}


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    for path in Path.home().joinpath("AppData/Local/Microsoft/WinGet/Packages").rglob(
        "ffmpeg.exe"
    ):
        return str(path)
    raise RuntimeError("找不到 ffmpeg")


@dataclass
class _Job:
    job_id: str
    source: Path | None = None          # 从资源池抽到的片段
    synth: dict | None = None           # 池子为空时现场合成
    dest: Path | None = None
    status: str = "queued"


@dataclass
class MockProvider:
    """本地"生成"：从资源池随机抽片段。零成本、零等待。"""

    pool: list[Path] = field(default_factory=list)   # 资源池里的所有片段
    # 优先抽的片段（比如分镜要求有人脸，就把带人脸的片段排在前面）
    preferred: list[Path] = field(default_factory=list)
    name: str = "mock"

    _jobs: dict[str, _Job] = field(default_factory=dict, init=False)
    # 一轮抽卡里不重复抽同一个片段，否则可能连抽 4 次同一张废片
    _queue: list[Path] = field(default_factory=list, init=False)

    # -- 接口实现 -------------------------------------------------------
    def capabilities(self) -> Capabilities:
        return Capabilities(
            max_duration_s=10.0,
            supports_seed=True,
            supports_image_to_video=True,
            supports_last_frame=False,
            supports_reference_images=False,
            resolutions=["480p"],
        )

    def estimate_cost_cents(self, req: GenRequest) -> float:
        return 0.0

    def submit(self, req: GenRequest) -> GenJobRef:
        job_id = f"mock-{uuid.uuid4().hex[:10]}"

        available = [p for p in self.pool if p.exists()]
        if available:
            if not self._queue:
                # 先把"匹配分镜期望"的片段洗好排前面，再从剩下的里补
                pref = [p for p in self.preferred if p in available]
                rest = [p for p in available if p not in pref]
                random.shuffle(pref)
                random.shuffle(rest)
                self._queue = pref + rest
            job = _Job(job_id=job_id, source=self._queue.pop(0))
        else:
            job = _Job(job_id=job_id, synth=self._random_recipe())

        job.status = "succeeded"        # mock 立即完成
        self._jobs[job_id] = job
        return GenJobRef(provider=self.name, job_id=job_id, raw={"prompt": req.prompt})

    def poll(self, ref: GenJobRef) -> str:
        job = self._jobs.get(ref.job_id)
        return job.status if job else "failed"

    def source_of(self, ref: GenJobRef) -> Path | None:
        """这次抽到的是池子里的哪个片段（现场合成的返回 None）。"""
        job = self._jobs.get(ref.job_id)
        return job.source if job else None

    def fetch(self, ref: GenJobRef, dest: Path) -> Path:
        job = self._jobs[ref.job_id]
        dest.parent.mkdir(parents=True, exist_ok=True)

        if job.source is not None:
            shutil.copyfile(job.source, dest)
        else:
            self._render(job.synth or self._random_recipe(), dest)
        job.dest = dest
        return dest

    # -- 内部 -----------------------------------------------------------
    @staticmethod
    def _random_recipe() -> dict:
        base = random.choice(list(BASE_SOURCES))
        if random.random() < 0.6:
            return {"base": base, "defect": "none", "severity": "none"}
        defect = random.choice(["blur", "noise", "flicker"])
        severity = random.choice(["light", "medium", "heavy"])
        return {"base": base, "defect": defect, "severity": severity}

    @staticmethod
    def _render(recipe: dict, dest: Path) -> None:
        base = recipe["base"]
        filters: list[str] = []
        hue = BASE_HUE.get(base, 0)
        if hue:
            filters.append(f"hue=h={hue}")

        defect = recipe.get("defect", "none")
        severity = recipe.get("severity", "none")
        if defect == "blur":
            filters.append(f"gblur=sigma={BLUR_SIGMA[severity]}:steps=1")
        elif defect == "noise":
            filters.append(f"noise=alls={NOISE_STRENGTH[severity]}:allf=t+u")
        elif defect == "flicker":
            filters.append(f"eq=eval=frame:brightness='{FLICKER_AMP[severity]}*sin(n*1.9)'")

        filters.append("format=yuv420p")
        cmd = [
            _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", BASE_SOURCES[base],
            "-t", "5", "-vf", ",".join(filters), "-r", str(FPS),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-movflags", "+faststart", str(dest),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"mock 合成失败：{result.stderr.strip()[:500]}")
