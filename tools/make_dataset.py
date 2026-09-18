#!/usr/bin/env python
"""生成合成测试集：8 个分镜 × 4 个候选，缺陷由我们自己注入。

核心价值
--------
缺陷是我们主动注入的，所以 **ground truth 是精确的**：哪条视频、哪一秒、
注入了什么缺陷、什么强度，全部记录在 labels.json 里。质检项准不准，
就是拿这个标签去算命中 / 漏检 / 误杀。

用法
----
    python tools/make_dataset.py            # 只生成缺失的
    python tools/make_dataset.py --force    # 全部重生成
    python tools/make_dataset.py --shots S007
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POOL_DIR = ROOT / "data" / "pool"
ASSET_DIR = ROOT / "data" / "face_assets"
LABELS_PATH = POOL_DIR / "labels.json"
SHOTS_CSV = ROOT / "data" / "shots_template.csv"

WIDTH, HEIGHT, FPS = 854, 480, 25
DEFAULT_DURATION = 5.0
DEFAULT_CANDIDATES = 4

# 优先级：内部 high/medium/low，表格里写中文
PRIORITY_LABELS = {"high": "高", "medium": "中", "low": "低"}

# ---------------------------------------------------------------------------
# ffmpeg 定位
# ---------------------------------------------------------------------------


def find_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    candidates = [
        Path.home() / "AppData/Local/Microsoft/WinGet/Packages",
    ]
    for base in candidates:
        if not base.exists():
            continue
        for path in base.rglob("ffmpeg.exe"):
            return str(path)
    raise SystemExit("找不到 ffmpeg，请先安装（winget install Gyan.FFmpeg）。")


FFMPEG = find_ffmpeg()


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    """一个候选视频的配方。"""

    base: str                       # 合成源名，或 "asset:portrait.jpg"
    defect: str = "none"            # none / blur / noise / flicker / freeze / black / spec
    severity: str = "none"          # none / light / medium / heavy
    note: str = ""
    tweak: str = ""                 # 额外的调色滤镜，让同一素材的不同候选看起来不一样

    @property
    def is_defective(self) -> bool:
        return self.defect != "none"


@dataclass
class ShotPlan:
    shot_id: str
    scene: str
    prompt: str
    expected_faces: int
    candidates: list[Candidate]
    spares: list[Candidate] = field(default_factory=list)
    expected_hands: int | None = None
    priority: str = "medium"
    candidates_per_shot: int = DEFAULT_CANDIDATES
    notes: str = ""


# 分镜的默认优先级和备注——写进模板，让表格开箱即用
SHOT_META: dict[str, tuple[str, str]] = {
    "S001": ("high", "开场镜头：需要 1 张人脸，注意五官不要崩"),
    "S002": ("high", "转折镜头：无人物，主体是猫"),
    "S003": ("medium", "细节镜头：慢推近，注意别抖"),
    "S004": ("high", "高潮镜头：需要 2 只手，手部最容易崩"),
    "S005": ("medium", "过渡镜头：大场景，注意运动连贯"),
    "S006": ("low", "收尾镜头：夜景，注意噪点"),
    "S007": ("medium", "测试用：候选应全部模糊（演示「需重抽」）"),
    "S008": ("medium", "测试用：候选应全部闪烁（演示「需重抽」）"),
}


# ---------------------------------------------------------------------------
# 缺陷配方：缺陷 → ffmpeg 滤镜
# ---------------------------------------------------------------------------

BLUR_SIGMA = {"light": 2.4, "medium": 4.5, "heavy": 8.0}
# 轻度噪点也要让人眼看得出来，否则人工无法复核算法判定
NOISE_STRENGTH = {"light": 22, "medium": 38, "heavy": 60}
FLICKER_AMP = {"light": 0.10, "medium": 0.18, "heavy": 0.26}
FREEZE_SECONDS = {"light": 1.0, "medium": 2.0, "heavy": 3.0}
BLACK_SECONDS = {"light": 0.4, "medium": 1.0, "heavy": 2.0}


def defect_filters(defect: str, severity: str) -> list[str]:
    """返回需要追加到滤镜链末尾的滤镜。"""
    if defect == "none":
        return []
    if defect == "blur":
        return [f"gblur=sigma={BLUR_SIGMA[severity]}:steps=1"]
    if defect == "noise":
        return [f"noise=alls={NOISE_STRENGTH[severity]}:allf=t+u"]
    if defect == "flicker":
        amp = FLICKER_AMP[severity]
        return [f"eq=eval=frame:brightness='{amp}*sin(n*1.9)'"]
    if defect == "black":
        dur = BLACK_SECONDS[severity]
        return [
            f"drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:"
            f"enable='between(t,1.5,{1.5 + dur})'"
        ]
    if defect == "freeze":
        # freeze 通过「缩短基础片段 + 末尾克隆」实现，在 render 里单独处理
        return []
    if defect == "spec":
        return []
    raise ValueError(f"未知缺陷：{defect}")


# ---------------------------------------------------------------------------
# 合成源
# ---------------------------------------------------------------------------

BASE_SOURCES = {
    # 分形缓慢缩放：有机、有运动、不会触发卡帧
    "mandelbrot": f"mandelbrot=size={WIDTH}x{HEIGHT}:rate={FPS}",
    # 平滑色场：变化必须够快，否则会被判成卡帧
    "gradients": f"gradients=size={WIDTH}x{HEIGHT}:rate={FPS}:speed=0.06",
    # 运动测试图案：细节丰富
    "testsrc2": f"testsrc2=size={WIDTH}x{HEIGHT}:rate={FPS}",
    # 细胞自动机：颗粒感强
    "life": f"life=size={WIDTH}x{HEIGHT}:rate={FPS}:mold=10:ratio=0.12:death_color=0x102030",
}

# 每个分镜给一点色相差异，视觉上不至于完全一样
BASE_HUE = {
    "mandelbrot": 0,
    "gradients": 30,
    "testsrc2": 200,
    "life": 120,
}


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def build_command(cand: Candidate, out_path: Path, duration: float, force: bool) -> list[str]:
    """拼出 ffmpeg 命令行。"""
    filters: list[str] = []

    # --- 输入 ---
    if cand.base.startswith("asset:"):
        asset = ASSET_DIR / cand.base.split(":", 1)[1]
        if not asset.exists():
            raise FileNotFoundError(f"素材不存在：{asset}")
        # 静态图 → 等比缩放后加边（不能裁剪，否则人像的脸会被裁掉导致检测不到），
        # 再用持续推镜制造足够的帧间变化——运动太小会被"卡帧"检测误判。
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error",
            "-y" if force else "-n",
            "-loop", "1", "-i", str(asset),
        ]
        frames_n = max(1, int(duration * FPS))
        filters.append(
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,"
            f"zoompan=z='1.02+0.28*on/{frames_n}':"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames_n}:"
            f"s={WIDTH}x{HEIGHT}:fps={FPS}"
        )
    else:
        if cand.base not in BASE_SOURCES:
            raise ValueError(f"未知合成源：{cand.base}")
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error",
            "-y" if force else "-n",
            "-f", "lavfi", "-i", BASE_SOURCES[cand.base],
        ]
        hue = BASE_HUE.get(cand.base, 0)
        if hue:
            filters.append(f"hue=h={hue}")

    # --- 缺陷 ---
    if cand.tweak:
        filters.append(cand.tweak)
    if cand.defect == "freeze":
        freeze_len = FREEZE_SECONDS[cand.severity]
        # 先截断到 (总时长 - 冻结时长)，再在末尾克隆最后一帧
        filters.append(f"trim=duration={max(0.5, duration - freeze_len):.3f},setpts=PTS-STARTPTS")
        filters.append(f"tpad=stop_mode=clone:stop_duration={freeze_len}")
    else:
        filters.extend(defect_filters(cand.defect, cand.severity))

    filters.append("format=yuv420p")

    cmd += [
        "-t", f"{duration:.3f}",
        "-vf", ",".join(filters),
        "-r", str(FPS),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-movflags", "+faststart",
        str(out_path),
    ]
    return cmd


def base_duration_for(cand: Candidate, duration: float) -> float:
    """freeze 缺陷靠 trim 截断基础片段，总时长仍由 -t 控制。"""
    if cand.defect == "spec":
        return duration * 0.5  # 规格不符：时长只有一半
    return duration


def spec_resolution_for(cand: Candidate) -> tuple[int, int]:
    if cand.defect == "spec":
        return 640, 360
    return WIDTH, HEIGHT


def render(cand: Candidate, out_path: Path, force: bool) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists() and not force:
        return

    duration = DEFAULT_DURATION
    if cand.defect == "spec":
        duration = DEFAULT_DURATION

    cmd = build_command(cand, out_path, base_duration_for(cand, duration), force)

    # 规格缺陷额外缩放分辨率
    if cand.defect == "spec":
        w, h = spec_resolution_for(cand)
        idx = cmd.index("-vf")
        cmd[idx + 1] = f"{cmd[idx + 1]},scale={w}:{h}"

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 渲染失败：{out_path.name}\n{result.stderr.strip()[:800]}"
        )


# ---------------------------------------------------------------------------
# 分镜计划
# ---------------------------------------------------------------------------

def build_plans() -> list[ShotPlan]:
    """分镜计划。

    重要约束：分镜声明的 `expected_faces` / `expected_hands` 必须和它的候选
    素材自洽——否则"期望有脸但候选里根本没脸"会让质检项 7/8 全线失分，
    诊断结果就会指向错误的方向。所以：
      * S001 期望 1 张脸 → 4 个候选全部来自人像素材
      * S004 期望 2 只手 → 4 个候选全部来自手部素材
      * 其余分镜期望 0 张脸，用合成源
    """
    plans = [
        # ---- 真实人脸素材分镜：让「人脸关键点」检测项真正有意义 ----
        ShotPlan(
            shot_id="S001", scene="开场",
            prompt="一个女孩在雨中的东京街头回头",
            expected_faces=1,
            candidates=[
                Candidate(base="asset:portrait.jpg", note="真人素材"),
                Candidate(base="asset:portrait.jpg",
                          tweak="eq=contrast=1.15:saturation=1.25",
                          note="真人素材（调色版本）"),
                Candidate(base="asset:portrait.jpg", defect="blur", severity="medium",
                          note="真人素材 + 模糊"),
                Candidate(base="asset:portrait.jpg", defect="noise", severity="medium",
                          note="真人素材 + 噪点"),
            ],
        ),
        ShotPlan(
            shot_id="S002", scene="转折",
            prompt="一只橘猫从窗台轻盈跳下",
            expected_faces=0,
            candidates=[
                Candidate(base="life"),
                Candidate(base="testsrc2"),
                Candidate(base="gradients", defect="blur", severity="light"),
                Candidate(base="testsrc2", defect="freeze", severity="medium"),
            ],
        ),
        ShotPlan(
            shot_id="S003", scene="细节",
            prompt="咖啡杯在木桌上冒着热气，镜头缓慢推近",
            expected_faces=0,
            candidates=[
                Candidate(base="mandelbrot"),
                Candidate(base="life"),
                Candidate(base="life", defect="noise", severity="heavy"),
                Candidate(base="gradients", defect="blur", severity="medium"),
            ],
        ),
        # ---- 真实手部素材分镜：让「手部关键点」检测项真正有意义 ----
        ShotPlan(
            shot_id="S004", scene="高潮",
            prompt="一个人双手捧着热咖啡的特写",
            expected_faces=0,
            expected_hands=2,
            candidates=[
                Candidate(base="asset:hands.jpg", note="真人手部素材"),
                Candidate(base="asset:hands.jpg",
                          tweak="eq=contrast=1.1:brightness=0.04",
                          note="真人手部素材（调色版本）"),
                Candidate(base="asset:hands.jpg", defect="flicker", severity="medium",
                          note="真人手部素材 + 闪烁"),
                Candidate(base="asset:hands.jpg", defect="black", severity="medium",
                          note="真人手部素材 + 黑屏"),
            ],
        ),
        ShotPlan(
            shot_id="S005", scene="过渡",
            prompt="老式火车穿过秋天的森林",
            expected_faces=0,
            candidates=[
                Candidate(base="testsrc2"),
                Candidate(base="mandelbrot"),
                Candidate(base="testsrc2", defect="blur", severity="heavy"),
                Candidate(base="gradients", defect="noise", severity="light"),
            ],
        ),
        ShotPlan(
            shot_id="S006", scene="收尾",
            prompt="街边小吃摊的烟火气，夜晚",
            expected_faces=0,
            candidates=[
                Candidate(base="life"),
                Candidate(base="testsrc2"),
                Candidate(base="life", defect="freeze", severity="light"),
                Candidate(base="testsrc2", defect="spec", severity="medium"),
            ],
        ),
        # --- 全否分镜 1：失败维度集中在「清晰度」---
        ShotPlan(
            shot_id="S007", scene="全否-清晰度",
            prompt="海边的吉他特写，镜头缓缓平移",
            expected_faces=0,
            candidates=[
                Candidate(base="mandelbrot", defect="blur", severity="heavy"),
                Candidate(base="gradients", defect="blur", severity="heavy"),
                Candidate(base="testsrc2", defect="blur", severity="heavy"),
                Candidate(base="life", defect="blur", severity="heavy"),
            ],
            spares=[
                # 备用素材以"能通过"为主，这样重抽后能明显看到状态好转。
                # 注意不要用 gradients：它是平滑色场，本身细节少，会被清晰度误判。
                Candidate(base="mandelbrot", note="备用：清晰"),
                Candidate(base="testsrc2", note="备用：清晰"),
                Candidate(base="life", note="备用：清晰"),
                Candidate(base="mandelbrot", defect="noise", severity="light",
                          note="备用：轻微噪点（会被判废）"),
            ],
        ),
        # --- 全否分镜 2：失败维度集中在「闪烁」---
        ShotPlan(
            shot_id="S008", scene="全否-闪烁",
            prompt="夕阳下的城市天际线延时",
            expected_faces=0,
            candidates=[
                Candidate(base="gradients", defect="flicker", severity="heavy"),
                Candidate(base="testsrc2", defect="flicker", severity="heavy"),
                Candidate(base="mandelbrot", defect="flicker", severity="heavy"),
                Candidate(base="life", defect="flicker", severity="heavy"),
            ],
            spares=[
                Candidate(base="mandelbrot", note="备用：稳定"),
                Candidate(base="testsrc2", note="备用：稳定"),
                Candidate(base="life", note="备用：稳定"),
                Candidate(base="testsrc2", defect="blur", severity="light",
                          note="备用：轻微模糊（会被判废）"),
            ],
        ),
    ]

    for plan in plans:
        meta = SHOT_META.get(plan.shot_id)
        if meta:
            plan.priority, plan.notes = meta
    return plans


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def render_candidate(cand: Candidate, out_path: Path, force: bool) -> str:
    """渲染单个候选，返回状态描述。"""
    if out_path.exists() and not force:
        return "skip"
    render(cand, out_path, force=True)
    return "ok"


def status_slug(cand: Candidate) -> str:
    """文件名里的状态标记：OK 表示好片，否则是「缺陷-严重度」。"""
    if cand.defect == "none":
        return "OK"
    return f"{cand.defect}-{cand.severity}"


def write_shots_csv(plans: list[ShotPlan]) -> None:
    """把内置分镜写成模板 CSV，界面上「下载分镜模板」拿的就是它。

    表头用中文（给人看），解析时中英文都认——顺序必须和
    app/dataset.py 的 FIELD_ORDER 一致。
    """
    header = [
        "分镜号", "场景", "提示词", "负面词", "时长(秒)", "画幅",
        "候选数", "参考图", "期望人脸数", "期望手部数", "优先级", "备注",
    ]
    rows = [header]
    for plan in plans:
        rows.append(
            [
                plan.shot_id,
                plan.scene,
                plan.prompt,
                "低质量,变形",           # 含逗号，必须由 csv 模块负责转义
                f"{DEFAULT_DURATION:g}",
                "16:9",
                str(plan.candidates_per_shot),
                "",
                "" if plan.expected_faces is None else str(plan.expected_faces),
                "" if plan.expected_hands is None else str(plan.expected_hands),
                PRIORITY_LABELS.get(plan.priority, "中"),
                plan.notes,
            ]
        )

    # 用 csv 模块写，避免字段里的逗号把列冲散
    with SHOTS_CSV.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成视频资源池")
    parser.add_argument("--force", action="store_true", help="清空资源池并全部重新生成")
    args = parser.parse_args()

    if args.force and POOL_DIR.exists():
        removed = 0
        for old in POOL_DIR.glob("*.mp4"):
            old.unlink()
            removed += 1
        if removed:
            print(f"已清空资源池（{removed} 个旧文件）")
    POOL_DIR.mkdir(parents=True, exist_ok=True)

    plans = build_plans()

    # 素材缺失时降级：把 asset: 候选换成合成源
    missing_assets: list[str] = []
    for plan in plans:
        for cand in plan.candidates + plan.spares:
            if cand.base.startswith("asset:"):
                name = cand.base.split(":", 1)[1]
                if not (ASSET_DIR / name).exists():
                    missing_assets.append(name)

    if missing_assets:
        print(f"⚠️  缺少素材 {sorted(set(missing_assets))}，相关候选将降级为合成源。")
        print("   运行 python tools/fetch_face_assets.py 可尝试自动拉取。\n")
        for plan in plans:
            for cand in plan.candidates + plan.spares:
                if cand.base.startswith("asset:"):
                    name = cand.base.split(":", 1)[1]
                    if not (ASSET_DIR / name).exists():
                        cand.base = "mandelbrot"
                        cand.note = (cand.note + " [已降级为合成源]").strip()

    clips: list[dict] = []
    total, made = 0, 0
    counter = 0

    for plan in plans:
        print(f"[{plan.shot_id}] {plan.prompt}")
        for cand in plan.candidates + plan.spares:
            counter += 1
            name = f"clip_{counter:02d}_{status_slug(cand)}.mp4"
            out = POOL_DIR / name
            total += 1

            if args.force or not out.exists():
                try:
                    render(cand, out, force=True)
                    made += 1
                except Exception as exc:  # noqa: BLE001 - 单个片段失败不该中断整批
                    print(f"   ✗ {name} 渲染失败：{exc}")
                    continue

            clips.append(
                {
                    "file": name,
                    "defects": [] if cand.defect == "none" else [cand.defect],
                    "severity": cand.severity,
                    "verdict": "reject" if cand.is_defective else "accept",
                    "base": cand.base,
                    "from_shot": plan.shot_id,
                    "note": cand.note,
                }
            )
            print(f"   {name}")

    labels = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "clips": clips}
    LABELS_PATH.write_text(
        json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_shots_csv(plans)

    print(f"\n完成：{made} 个新渲染 / 资源池共 {len(clips)} 个片段")
    print(f"资源池：{POOL_DIR}")
    print(f"标签：  {LABELS_PATH}")
    print(f"模板：  {SHOTS_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
