#!/usr/bin/env python
"""阈值校准：把每个候选的**原始指标**dump 出来，按注入的缺陷分组。

为什么需要它
------------
质检准不准，取决于每个指标的原始数值能不能把"好片"和"坏片"分开。
这个脚本不看分数，只看原始值，因此可以据此重新设定阈值——这正是界面上
那些滑块背后的校准依据。

用法
----
    python tools/calibrate.py
    python tools/calibrate.py --csv data/calibration.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402


def raw_metrics(path: Path) -> dict[str, float]:
    """直接算原始指标，不经过检测项的阈值映射。"""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return {}
    fps = float(cap.get(cv2.CAP_PROP_FPS)) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    frames: list[np.ndarray] = []
    step = max(1, math.ceil(total / 48)) if total > 0 else 1
    idx = 0
    while idx < total and len(frames) < 48:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(frame)
        idx += step
    cap.release()

    if not frames:
        return {}

    gray = np.array([cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames], dtype=np.float32)

    # 黑屏/纯色
    means = gray.reshape(len(gray), -1).mean(axis=1)
    stds = gray.reshape(len(gray), -1).std(axis=1)
    bad = ((means < 16) & (stds < 16)) | (stds < 6)

    # 卡帧
    diffs = np.abs(np.diff(gray, axis=0)).reshape(len(gray) - 1, -1).mean(axis=1)

    # 卡帧（归一化版）：消除"对比度低"造成的假性静止
    centered = gray - gray.mean(axis=(1, 2), keepdims=True)
    scale = centered.std(axis=(1, 2), keepdims=True) + 1e-6
    ndiffs = np.abs(np.diff(centered / scale, axis=0)).reshape(len(gray) - 1, -1).mean(axis=1)

    # 闪烁
    brightness = gray.reshape(len(gray), -1).mean(axis=1)
    second = np.abs(np.diff(brightness, n=2)) if len(gray) >= 3 else np.array([0.0])

    # 噪点（中值滤波残差）
    residuals = [float(cv2.absdiff(f, cv2.medianBlur(f, 3)).mean()) for f in frames]

    # 噪点（只看平坦区域的高频能量）：结构化细节（细胞自动机、分形）不会污染这个估计
    flat_noise: list[float] = []
    for f in frames:
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32)
        lap = cv2.Laplacian(g, cv2.CV_32F)
        grad = (
            np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0))
            + np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1))
        )
        mask = grad <= np.percentile(grad, 25)
        if mask.sum() >= 100:
            flat_noise.append(float(np.abs(lap[mask]).mean()))

    # 清晰度（拉普拉斯方差）
    lap = [float(cv2.Laplacian(f, cv2.CV_32F).var()) for f in gray]

    # 内容无关的模糊指标：再糊一次，看高频掉了多少。
    # 清晰图再糊会掉很多（比值小）；本来就糊的图再糊几乎不变（比值接近 1）。
    blur_ratio = []
    for f in gray:
        lap0 = float(cv2.Laplacian(f, cv2.CV_32F).var())
        if lap0 < 1e-6:
            blur_ratio.append(1.0)
            continue
        ref = cv2.GaussianBlur(f, (0, 0), 1.5)
        lap1 = float(cv2.Laplacian(ref, cv2.CV_32F).var())
        blur_ratio.append(min(1.5, lap1 / lap0))

    # 清晰度的"内容无关"版本：高频能量 / 整体对比度
    hf = []
    for f in gray:
        lap_abs = float(np.abs(cv2.Laplacian(f, cv2.CV_32F)).mean())
        contrast = float(f.std()) + 1e-6
        hf.append(lap_abs / contrast)

    return {
        "w": width,
        "h": height,
        "dur": round(total / fps, 2) if fps else 0,
        "bad_ratio": round(float(bad.mean()), 4),
        "mean_std": round(float(stds.mean()), 2),
        "diff_med": round(float(np.median(diffs)) if len(diffs) else 0, 3),
        "diff_p10": round(float(np.percentile(diffs, 10)) if len(diffs) else 0, 3),
        "frozen_ratio": round(float((diffs < 1.6).mean()) if len(diffs) else 0, 4),
        "frozen_raw": round(float((diffs < 0.05).mean()) if len(diffs) else 0, 4),
        "freeze_n": round(float(np.median(ndiffs)) if len(ndiffs) else 0, 4),
        "frozen_n": round(float((ndiffs < 0.06).mean()) if len(ndiffs) else 0, 4),
        "flicker_e": round(float(second.mean()), 3),
        "flicker_p": round(float(second.max()), 3),
        "swing": round(float(brightness.max() - brightness.min()), 2),
        "noise": round(float(np.median(residuals)), 3),
        "noise_flat": round(float(np.median(flat_noise)) if flat_noise else 0, 3),
        "lapvar": round(float(np.median(lap)), 1),
        "blur_r": round(float(np.median(blur_ratio)), 3),
        "hf_ratio": round(float(np.median(hf)), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", help="把结果额外写成 CSV")
    args = parser.parse_args()

    labels = json.loads(config.POOL_LABELS.read_text(encoding="utf-8"))
    groups: dict[str, list[tuple[str, dict]]] = defaultdict(list)

    for clip in labels.get("clips", []):
        path = config.POOL_DIR / clip["file"]
        if not path.exists():
            continue
        metrics = raw_metrics(path)
        defects = clip.get("defects") or []
        key = ",".join(defects) or "good"
        key = f"{key}/{clip.get('severity', 'none')}" if defects else key
        groups[key].append((clip["file"], metrics))

    cols = [
        "bad_ratio", "diff_med", "frozen_raw", "flicker_e",
        "noise", "noise_flat", "lapvar", "blur_r",
    ]

    print(f"{'组别':<22s} {'n':>3s}  " + "  ".join(f"{c:>10s}" for c in cols))
    print("-" * (26 + 12 * len(cols)))
    for key in sorted(groups):
        rows = groups[key]
        print(f"{key:<22s} {len(rows):>3d}  " + "  ".join(
            f"{np.median([r[1].get(c, 0) for r in rows]):>10.3f}" for c in cols
        ))

    if args.csv:
        out = Path(args.csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(
                fh, fieldnames=["candidate_id", "group"] + cols, extrasaction="ignore"
            )
            writer.writeheader()
            for key, rows in groups.items():
                for cid, metrics in rows:
                    writer.writerow({"candidate_id": cid, "group": key, **metrics})
        print(f"\n已写出：{out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
