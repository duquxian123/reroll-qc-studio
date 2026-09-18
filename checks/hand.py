"""手部关键点检测（软性项，最低权重、无否决权）。

用 MediaPipe Hands 检测手部关键点。手部是 AI 视频最显眼的崩坏点之一
（多指、断指、手指融合），所以这一项必须接入。

⚠️ demo 阶段这一项**未校准**：合成素材里没有真人手，分数只用于证明
"这一项已接入且能跑出结果"。真实素材到位后调阈值即可。

判据：
  * 检出率：期望有手的分镜里，有多少帧真的检出了手
  * 手部数量：与分镜表 expected_hands 是否一致
  * 手指张开度：伸出指数是否落在合理范围
  * 关键点稳定性：跨帧抖动
"""

from __future__ import annotations

import base64
import threading
from typing import Any

import cv2
import numpy as np

from .base import CheckResult, Evidence, FrameCache, clamp_score, ramp
from .registry import register

MAX_MODEL_FRAMES = 12

# MediaPipe Hands 关键点索引
_WRIST = 0
_FINGERS = (
    (4, 3),    # 拇指 (tip, pip)
    (8, 6),    # 食指
    (12, 10),  # 中指
    (16, 14),  # 无名指
    (20, 18),  # 小指
)
_CONNECTIONS = (
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20),
)

_lock = threading.Lock()
_hands = None


def _get_hands():
    """懒加载单例。"""
    global _hands
    if _hands is None:
        with _lock:
            if _hands is None:
                import mediapipe as mp

                _hands = mp.solutions.hands.Hands(
                    static_image_mode=True,
                    max_num_hands=4,
                    min_detection_confidence=0.5,
                )
    return _hands


class HandCheck:
    id = "hand"
    name = "手部关键点"
    description = "MediaPipe Hands 检测手部数量、手指张开度与跨帧稳定性。demo 阶段未校准。"
    unit = "分"
    hard = False
    default_weight = 0.5
    default_threshold = 70.0
    pass_when_above = True
    requires_models = True
    # 默认不勾选：MediaPipe 在 CPU 上较慢，而且这一项还没校准
    default_enabled = False

    def run(self, cache: FrameCache, params: dict[str, Any]) -> CheckResult:
        expected = params.get("expected_hands")
        if expected is not None:
            expected = int(expected)

        frames = cache.frames
        idxs = _sample_indices(len(frames), MAX_MODEL_FRAMES)
        model = _get_hands()

        counts: list[int] = []
        ext_ratios: list[float] = []
        landmarks_by_frame: list[np.ndarray | None] = []
        overlay: tuple[int, np.ndarray] | None = None

        for i in idxs:
            frame = frames[i]
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            rgb.flags.writeable = False
            result = model.process(rgb)
            hands = result.multi_hand_landmarks or []
            counts.append(len(hands))

            if hands:
                pts = np.array(
                    [[lm.x, lm.y] for lm in hands[0].landmark], dtype=np.float32
                )
                landmarks_by_frame.append(pts)
                ext_ratios.append(_extended_finger_ratio(pts))
                if overlay is None:
                    overlay = (i, _draw_overlay(frame, pts))
            else:
                landmarks_by_frame.append(None)

        det_rate = float(np.mean([c > 0 for c in counts])) if counts else 0.0
        mean_count = float(np.mean(counts)) if counts else 0.0
        ext_ratio = float(np.median(ext_ratios)) if ext_ratios else None
        stability = _stability(landmarks_by_frame)

        # --- 打分 ---
        if expected is None:
            if det_rate == 0.0:
                score = 100.0
                verdict_note = "分镜未要求手部，且未检出手部，视为通过"
            else:
                score = _extension_score(ext_ratio)
                verdict_note = f"检出手部 {mean_count:.1f} 个/帧，手指张开度得分 {score:.0f}"
        elif expected == 0:
            score = clamp_score(100.0 * (1.0 - det_rate))
            verdict_note = (
                "未检出手部，符合预期"
                if det_rate == 0.0
                else f"分镜期望 0 只手，但有 {det_rate:.0%} 的帧检出了手"
            )
        else:
            hit = float(np.mean([c >= 1 for c in counts])) if counts else 0.0
            exact = float(np.mean([c == expected for c in counts])) if counts else 0.0
            base = 100.0 * (0.7 * hit + 0.3 * exact)
            score = clamp_score(0.8 * base + 0.2 * _extension_score(ext_ratio))
            verdict_note = f"期望 {expected} 只手；检出率 {hit:.0%}，数量吻合率 {exact:.0%}"

        if stability is not None and stability > 0.05:
            penalty = ramp(stability, bad=0.30, good=0.05)
            score = clamp_score(score * (0.5 + 0.5 * penalty / 100.0))
            verdict_note += f"；关键点跨帧抖动 {stability:.3f}"

        passed = score >= 70.0

        detail = {
            "expected_hands": expected,
            "model_frames": len(idxs),
            "detection_rate": round(det_rate, 4),
            "mean_hand_count": round(mean_count, 2),
            "extended_finger_ratio": None if ext_ratio is None else round(ext_ratio, 4),
            "landmark_stability": None if stability is None else round(stability, 4),
            "calibrated": False,
        }

        evidence: list[Evidence] = []
        if overlay is not None:
            evidence.append(
                Evidence(
                    kind="overlay",
                    label="手部骨架",
                    frame_index=cache.frame_index_of(overlay[0]),
                    data={"image_b64": _encode_jpeg_b64(overlay[1])},
                )
            )

        return CheckResult(
            check_id=self.id,
            score=score,
            passed=passed,
            detail=detail,
            evidence=evidence,
            notes=verdict_note,
        )


def _sample_indices(total: int, limit: int) -> list[int]:
    if total <= limit:
        return list(range(total))
    return sorted(set(np.linspace(0, total - 1, limit).astype(int).tolist()))


def _extended_finger_ratio(pts: np.ndarray) -> float:
    """5 根手指里有多少根是"伸出"状态（指尖离手腕比指中关节更远）。"""
    wrist = pts[_WRIST]
    extended = 0
    for tip, pip in _FINGERS:
        if np.linalg.norm(pts[tip] - wrist) > np.linalg.norm(pts[pip] - wrist):
            extended += 1
    return extended / len(_FINGERS)


def _extension_score(ext_ratio: float | None) -> float:
    """张开的正常手在 0.4-1.0 之间；全握拳 0 也可接受，中间地带反而可疑。"""
    if ext_ratio is None:
        return 60.0
    if ext_ratio >= 0.4 or ext_ratio <= 0.05:
        return 100.0
    return ramp(ext_ratio, bad=0.2, good=0.4)


def _stability(landmarks: list[np.ndarray | None]) -> float | None:
    deltas: list[float] = []
    for prev, cur in zip(landmarks, landmarks[1:]):
        if prev is None or cur is None or prev.shape != cur.shape:
            continue
        deltas.append(float(np.linalg.norm(prev - cur, axis=1).mean()))
    if not deltas:
        return None
    return float(np.median(deltas))


def _draw_overlay(frame: np.ndarray, pts: np.ndarray) -> np.ndarray:
    vis = frame.copy()
    h, w = vis.shape[:2]
    for a, b in _CONNECTIONS:
        p1 = (int(pts[a, 0] * w), int(pts[a, 1] * h))
        p2 = (int(pts[b, 0] * w), int(pts[b, 1] * h))
        cv2.line(vis, p1, p2, (0, 255, 0), 2)
    for x, y in pts:
        cv2.circle(vis, (int(x * w), int(y * h)), 2, (0, 200, 255), -1)
    return vis


def _encode_jpeg_b64(img: np.ndarray) -> str:
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    if not ok:
        return ""
    return base64.b64encode(buf.tobytes()).decode("ascii")


register(HandCheck())
