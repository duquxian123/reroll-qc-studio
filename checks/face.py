"""人脸关键点检测（软性项，最低权重、无否决权）。

用 MediaPipe FaceMesh 在抽帧上检测人脸关键点。

⚠️ demo 阶段这一项**未校准**：合成素材里没有真人脸，所以它的分数只用来
证明"这一项已接入且能跑出结果"，不参与校准过的准确率统计。真实素材到位后
只需要在界面上调阈值即可。

判据：
  * 检出率：期望有人脸的分镜里，有多少帧真的检出了人脸
  * 人脸数量：与分镜表 expected_faces 是否一致
  * 五官比例：双眼间距 / 人脸框宽度，偏离正常区间则扣分
  * 关键点稳定性：跨帧关键点的归一化抖动
"""

from __future__ import annotations

import threading
from typing import Any

import cv2
import numpy as np

from .base import CheckResult, Evidence, FrameCache, clamp_score, ramp
from .registry import register

# 最多在多少帧上跑模型：CPU 上跑满所有帧太慢，人脸检测不需要那么密
MAX_MODEL_FRAMES = 12

# MediaPipe FaceMesh 关键点索引
_LEFT_EYE_OUTER = 33
_RIGHT_EYE_OUTER = 263

_lock = threading.Lock()
_face_mesh = None


def _get_face_mesh():
    """懒加载单例。FaceMesh 初始化要读模型文件，别每次检测都重建。"""
    global _face_mesh
    if _face_mesh is None:
        with _lock:
            if _face_mesh is None:
                import mediapipe as mp

                _face_mesh = mp.solutions.face_mesh.FaceMesh(
                    static_image_mode=True,
                    max_num_faces=4,
                    refine_landmarks=False,
                    min_detection_confidence=0.5,
                )
    return _face_mesh


class FaceCheck:
    id = "face"
    name = "人脸关键点"
    description = "MediaPipe FaceMesh 检测人脸数量、五官比例与跨帧稳定性。demo 阶段未校准。"
    unit = "分"
    hard = False
    default_weight = 0.5
    default_threshold = 70.0
    pass_when_above = True
    requires_models = True
    # 默认不勾选：MediaPipe 在 CPU 上较慢，而且这一项还没校准
    default_enabled = False

    def run(self, cache: FrameCache, params: dict[str, Any]) -> CheckResult:
        expected = params.get("expected_faces")
        if expected is not None:
            expected = int(expected)
        iou_bad = float(params.get("eye_ratio_bad", 0.14))
        iou_good = float(params.get("eye_ratio_good", 0.22))

        frames = cache.frames
        idxs = _sample_indices(len(frames), MAX_MODEL_FRAMES)
        mesh = _get_face_mesh()

        counts: list[int] = []
        eye_ratios: list[float] = []
        landmarks_by_frame: list[np.ndarray | None] = []
        best_overlay: tuple[int, np.ndarray] | None = None

        for i in idxs:
            frame = frames[i]
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            rgb.flags.writeable = False
            result = mesh.process(rgb)
            faces = result.multi_face_landmarks or []
            counts.append(len(faces))

            if faces:
                pts = np.array(
                    [[lm.x, lm.y] for lm in faces[0].landmark], dtype=np.float32
                )
                landmarks_by_frame.append(pts)
                w = float(pts[:, 0].max() - pts[:, 0].min())
                if w > 1e-4:
                    eye = float(
                        np.hypot(
                            pts[_LEFT_EYE_OUTER, 0] - pts[_RIGHT_EYE_OUTER, 0],
                            pts[_LEFT_EYE_OUTER, 1] - pts[_RIGHT_EYE_OUTER, 1],
                        )
                    )
                    eye_ratios.append(eye / w)
                if best_overlay is None:
                    best_overlay = (i, _draw_overlay(frame, pts, w))
            else:
                landmarks_by_frame.append(None)

        det_rate = float(np.mean([c > 0 for c in counts])) if counts else 0.0
        mean_count = float(np.mean(counts)) if counts else 0.0
        eye_ratio = float(np.median(eye_ratios)) if eye_ratios else None
        stability = _stability(landmarks_by_frame)

        # --- 打分 ---
        if expected is None:
            # 分镜没要求人脸：检出就检查五官比例，没检出就满分（不关心）
            if det_rate == 0.0:
                score = 100.0
                verdict_note = "分镜未要求人脸，且未检出人脸，视为通过"
            else:
                score = _eye_ratio_score(eye_ratio, iou_bad, iou_good)
                verdict_note = f"检出人脸 {mean_count:.1f} 个/帧，五官比例得分 {score:.0f}"
        elif expected == 0:
            # 不该有人脸却检出了 → 多余人脸
            score = clamp_score(100.0 * (1.0 - det_rate))
            verdict_note = (
                "未检出人脸，符合预期"
                if det_rate == 0.0
                else f"分镜期望 0 张脸，但有 {det_rate:.0%} 的帧检出了人脸"
            )
        else:
            hit = float(np.mean([c >= 1 for c in counts])) if counts else 0.0
            exact = float(np.mean([c == expected for c in counts])) if counts else 0.0
            base = 100.0 * (0.7 * hit + 0.3 * exact)
            eye_score = _eye_ratio_score(eye_ratio, iou_bad, iou_good)
            score = clamp_score(0.75 * base + 0.25 * eye_score)
            verdict_note = (
                f"期望 {expected} 张脸；检出率 {hit:.0%}，数量吻合率 {exact:.0%}"
            )

        # 跨帧抖动：不稳就扣分
        if stability is not None and stability > 0.05:
            penalty = ramp(stability, bad=0.25, good=0.05)
            score = clamp_score(score * (0.5 + 0.5 * penalty / 100.0))
            verdict_note += f"；关键点跨帧抖动 {stability:.3f}"

        passed = score >= 70.0

        detail = {
            "expected_faces": expected,
            "model_frames": len(idxs),
            "detection_rate": round(det_rate, 4),
            "mean_face_count": round(mean_count, 2),
            "eye_ratio": None if eye_ratio is None else round(eye_ratio, 4),
            "landmark_stability": None if stability is None else round(stability, 4),
            "calibrated": False,
        }

        evidence: list[Evidence] = []
        if best_overlay is not None:
            evidence.append(
                Evidence(
                    kind="overlay",
                    label="人脸关键点",
                    frame_index=cache.frame_index_of(best_overlay[0]),
                    image_path=None,
                    data={"image_b64": _encode_jpeg_b64(best_overlay[1])},
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


def _eye_ratio_score(eye_ratio: float | None, bad: float, good: float) -> float:
    """双眼间距 / 人脸宽度，正常约 0.2-0.35。偏离越多分越低。"""
    if eye_ratio is None:
        return 60.0
    if eye_ratio < bad:
        return ramp(eye_ratio, bad=0.0, good=bad)
    if eye_ratio > 0.6:
        return ramp(eye_ratio, bad=0.9, good=0.6)
    return 100.0


def _stability(landmarks: list[np.ndarray | None]) -> float | None:
    """相邻帧之间关键点位移的中位数（已归一化到 0-1 坐标）。"""
    deltas: list[float] = []
    for prev, cur in zip(landmarks, landmarks[1:]):
        if prev is None or cur is None:
            continue
        if prev.shape != cur.shape:
            continue
        deltas.append(float(np.linalg.norm(prev - cur, axis=1).mean()))
    if not deltas:
        return None
    return float(np.median(deltas))


def _draw_overlay(frame: np.ndarray, pts: np.ndarray, face_width: float) -> np.ndarray:
    """把关键点画在帧上，作为可复核证据。"""
    vis = frame.copy()
    h, w = vis.shape[:2]
    for x, y in pts:
        cv2.circle(vis, (int(x * w), int(y * h)), 1, (0, 255, 0), -1)
    for a, b in ((_LEFT_EYE_OUTER, _RIGHT_EYE_OUTER),):
        p1 = (int(pts[a, 0] * w), int(pts[a, 1] * h))
        p2 = (int(pts[b, 0] * w), int(pts[b, 1] * h))
        cv2.line(vis, p1, p2, (0, 200, 255), 2)
    cv2.putText(
        vis, f"face w={face_width:.2f}", (8, 22),
        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2,
    )
    return vis


def _encode_jpeg_b64(img: np.ndarray) -> str:
    import base64

    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    if not ok:
        return ""
    return base64.b64encode(buf.tobytes()).decode("ascii")


register(FaceCheck())
