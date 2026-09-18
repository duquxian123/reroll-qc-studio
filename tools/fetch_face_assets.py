#!/usr/bin/env python
"""拉取公开的人脸 / 手部测试素材。

为什么需要这个
--------------
ffmpeg 合成不出真人脸和手，而「人脸关键点」「手部关键点」两个检测项
必须有真实素材才能跑出结果。这里下载 MediaPipe 官方仓库使用的测试图片
（属于 Apache-2.0 许可的 google-ai-edge/mediapipe 项目资产），本地转成短视频。

如果下载失败（网络/代理问题），脚本会安静退出，测试集会退化成纯合成素材，
人脸/手部两项会显示"未检出"——这也是我们在规格里写好的回退方案。
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "face_assets"

# MediaPipe 官方文档与示例使用的测试图，Apache-2.0
ASSETS: list[tuple[str, str]] = [
    ("portrait.jpg", "https://storage.googleapis.com/mediapipe-assets/portrait.jpg"),
    ("hands.jpg", "https://storage.googleapis.com/mediapipe-tasks/hand_landmarker/woman_hands.jpg"),
]

TIMEOUT = 30


def download(name: str, url: str, dest_dir: Path) -> bool:
    dest = dest_dir / name
    if dest.exists() and dest.stat().st_size > 1024:
        print(f"  [跳过] {name} 已存在")
        return True
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "reroll-studio/0.1"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        print(f"  [失败] {name}：{exc}")
        return False

    if len(data) < 1024:
        print(f"  [失败] {name}：下载内容过小")
        return False

    dest.write_bytes(data)
    print(f"  [成功] {name}（{len(data) / 1024:.0f} KB）")
    return True


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("拉取公开人脸/手部素材：")
    ok = sum(download(name, url, OUT_DIR) for name, url in ASSETS)
    if ok == 0:
        print("→ 没有拉到任何素材，人脸/手部检测项将显示「未检出」。")
        return 0
    print(f"→ 完成，共 {ok}/{len(ASSETS)} 个素材。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
