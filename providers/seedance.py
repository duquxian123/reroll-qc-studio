"""火山方舟 Seedance Provider —— 骨架实现。

**当前状态：不发起任何网络请求。** 只把接口和字段映射写好，
你拿到 API key 之后填进环境变量 `ARK_API_KEY` 就能用。

接口对应关系（火山方舟「内容生成异步任务」）：

    POST /api/v3/contents/generations/tasks          ← submit()
    GET  /api/v3/contents/generations/tasks/{id}     ← poll()
    下载返回的视频 URL                                ← fetch()

字段映射：

    统一字段            → 方舟字段
    prompt              → content[] 中的 {"type":"text"}
    ref_image           → content[] 中 role: first_frame 的 image_url
    duration_s          → duration
    aspect_ratio        → ratio
    resolution          → resolution
    seed                → seed
    watermark           → watermark

三个必须记住的坑：
  1. 返回的视频 URL 是**临时签名地址**，必须立刻下载，否则过期
  2. 任务状态是 queued/pending → running → succeeded/failed/canceled
  3. **seed 不一定能固定复现**，重抽要靠多抽而不是改 seed
"""

from __future__ import annotations

import os
from pathlib import Path

from .base import Capabilities, GenJobRef, GenRequest

DEFAULT_ENDPOINT = "https://ark.cn-beijing.volces.com"
CREATE_PATH = "/api/v3/contents/generations/tasks"
QUERY_PATH = "/api/v3/contents/generations/tasks/{task_id}"

# 模型标识可能随火山引擎更新，可通过环境变量覆盖
DEFAULT_MODEL = "doubao-seedance-2-0-260128"

# 参考单价（分/秒），仅用于预算估算，实际以账单为准
PRICE_CENTS_PER_SECOND = {
    "480p": 30.0,
    "720p": 60.0,
    "1080p": 100.0,
}


class SeedanceProvider:
    name = "seedance"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        endpoint: str = DEFAULT_ENDPOINT,
    ) -> None:
        self.api_key = api_key or os.environ.get("ARK_API_KEY", "")
        self.model = model or os.environ.get("ARK_MODEL", DEFAULT_MODEL)
        self.endpoint = endpoint.rstrip("/")
        self._jobs: dict[str, dict] = {}

    # -- 能力与成本 -----------------------------------------------------
    def capabilities(self) -> Capabilities:
        return Capabilities(
            max_duration_s=30.0,
            supports_seed=True,
            supports_image_to_video=True,
            supports_last_frame=True,
            supports_reference_images=True,
            resolutions=["480p", "720p", "1080p"],
        )

    def estimate_cost_cents(self, req: GenRequest) -> float:
        unit = PRICE_CENTS_PER_SECOND.get(req.resolution, 60.0)
        return unit * float(req.duration_s)

    # -- 请求构造（可直接复用，已按方舟协议映射） -----------------------
    def build_payload(self, req: GenRequest) -> dict:
        content: list[dict] = [{"type": "text", "text": self._compose_prompt(req)}]

        if req.ref_image:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": req.ref_image},
                    "role": "first_frame",
                }
            )

        payload: dict = {
            "model": self.model,
            "content": content,
            "duration": int(req.duration_s),
            "ratio": req.aspect_ratio,
            "resolution": req.resolution,
            "watermark": req.watermark,
        }
        if req.seed is not None:
            payload["seed"] = int(req.seed)
        payload.update(req.extra)
        return payload

    @staticmethod
    def _compose_prompt(req: GenRequest) -> str:
        if req.negative_prompt:
            return f"{req.prompt}  --no {req.negative_prompt}"
        return req.prompt

    # -- 接口实现 -------------------------------------------------------
    def submit(self, req: GenRequest) -> GenJobRef:
        self._require_key()
        raise NotImplementedError(
            "SeedanceProvider 目前是骨架：字段映射已就绪，但尚未发起真实请求。\n"
            "接入步骤：\n"
            "  1. 设置环境变量 ARK_API_KEY\n"
            "  2. 在本文件里用 httpx/requests 实现 submit / poll / fetch\n"
            "  3. 把 app 里的 provider 从 MockProvider 换成 SeedanceProvider"
        )

    def poll(self, ref: GenJobRef) -> str:
        self._require_key()
        raise NotImplementedError("见 submit() 的接入步骤。")

    def fetch(self, ref: GenJobRef, dest: Path) -> Path:
        self._require_key()
        raise NotImplementedError("见 submit() 的接入步骤。")

    def _require_key(self) -> None:
        if not self.api_key:
            raise RuntimeError(
                "缺少 ARK_API_KEY。Seedance Provider 已就位，但需要 API key 才能调用。"
            )
