"""FastAPI 应用：审片工作台的后端。

工作流（和界面一一对应）
------------------------
1. 下载分镜模板 → 填好 → 导入分镜表
2. 点「抽卡」→ 从视频资源池随机抽片段，作为该分镜的候选
   （真实场景下这一步是调用云端生成 API）
3. 手动触发质检 → 只跑"待质检"的分镜
4. 人工判断 / 一键采纳得分最高的候选

分镜状态由 app/runtime.py 统一判定，这里只负责接口。
"""

from __future__ import annotations

import mimetypes
import shutil
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from checks import describe_all
from providers import MockProvider
from providers.base import GenRequest

from . import config, deliver, progress
from .dataset import (
    DEFAULT_CANDIDATES_PER_SHOT,
    SUPPORTED_SUFFIXES,
    Project,
    blank_project,
    import_csv,
    load_builtin,
    load_pool,
    parse_table,
)
from .models import Candidate, CheckConfig
from .orchestrator import (
    DEFAULT_PASS_LINE,
    DEFAULT_SOFT_TOLERANCE,
    Orchestrator,
    summarize,
)
from .project_store import load_project
from .runtime import Runtime, parse_configs
from .store import SessionState
from .template import build_workbook

app = FastAPI(title="Reroll Studio", version="0.3.0")

# 全局运行态（demo 阶段单项目）
runtime = Runtime(SessionState.load())
if runtime.project is None:
    # 先试上次落盘的项目（分镜 + 候选），没有才回退到内置示例分镜
    runtime.set_project(load_project() or load_builtin())


def _prune_session() -> None:
    """清掉指向"已经不存在的候选"的采纳记录。

    正常情况下项目会从 state/project.json 恢复，采纳记录不会悬空；
    但池子被重建、候选文件消失时仍然可能对不上。如果不清掉，下次抽卡
    抽到同名片段时，它会被误当成"早就采纳过"，导出时就会拿一条没质检过
    的片段冒充采纳结果。
    """
    project = runtime.require_project()
    known = {shot.shot_id: {c.candidate_id for c in shot.candidates} for shot in project.shots}
    stale = [
        shot_id
        for shot_id, candidate_id in list(runtime.session.adopted.items())
        if candidate_id not in known.get(shot_id, set())
    ]
    for shot_id in stale:
        runtime.session.clear_shot(shot_id)
    if stale:
        runtime.save()


_prune_session()


# ---------------------------------------------------------------------------
# 抽卡：从资源池随机取片段
# ---------------------------------------------------------------------------


def _clip_matches(clip, shot) -> bool:
    """这个片段是否符合分镜对人脸 / 手部的期望。

    真实场景里，API 返回的候选大体是照提示词生成的，不会给"要有脸"的镜头
    返回一个没人脸的片段。所以抽卡时优先抽匹配的片段，剩下的再兜底。
    """
    faces = 1 if "portrait" in clip.base else 0
    hands = 2 if "hands" in clip.base else 0

    if shot.expected_faces:
        if faces < shot.expected_faces:
            return False
    elif shot.expected_faces == 0 and faces:
        return False

    if shot.expected_hands and hands < shot.expected_hands:
        return False
    return True


def _make_provider(shot=None) -> MockProvider:
    clips = load_pool()
    preferred = [config.POOL_DIR / c.name for c in clips if shot is not None and _clip_matches(c, shot)]
    return MockProvider(
        pool=[config.POOL_DIR / c.name for c in clips],
        preferred=preferred,
    )


def _clamp_int(value: Any, lo: int, hi: int, default: Any) -> Any:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(number, hi))


def _clamp_float(value: Any, lo: float, hi: float, default: Any) -> Any:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(number, hi))


def _draw_count_for(shot, override: int | None = None) -> int:
    """这个分镜抽几条候选。

    优先级：接口显式传的 count > 分镜表里的「候选数」> 默认 4。
    以前是写死的 4，现在分镜表里可以逐镜配置。
    """
    raw = override if override else shot.candidates_per_shot
    try:
        number = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_CANDIDATES_PER_SHOT
    return max(1, min(number, 12))


def _draw_candidates(shot, count: int) -> list[Candidate]:
    """给一个分镜抽 count 个候选。

    抽到的片段直接引用资源池里的文件（不复制），候选 id 就是文件名去掉扩展名，
    所以界面上看到的 id 里已经带着这个片段的真实状态。
    """
    provider = _make_provider(shot)
    clips = {c.name: c for c in load_pool()}
    out: list[Candidate] = []

    for i in range(1, count + 1):
        req = GenRequest(
            shot_id=shot.shot_id,
            prompt=shot.prompt,
            negative_prompt=shot.negative_prompt,
            duration_s=shot.expected_duration_s or 5.0,
            aspect_ratio=shot.aspect_ratio or "16:9",
            resolution="480p",
        )
        ref = provider.submit(req)
        src = provider.source_of(ref)

        if src is None:
            # 资源池为空时的兜底：本地现场合成
            dest = config.DATA_DIR / "samples" / shot.shot_id / f"gen_{i:02d}.mp4"
            provider.fetch(ref, dest)
            out.append(
                Candidate(
                    candidate_id=f"{shot.shot_id}_gen{i}",
                    file=str(dest.relative_to(config.DATA_DIR)).replace("\\", "/"),
                    shot_id=shot.shot_id,
                    provider=provider.name,
                    note="本地合成",
                )
            )
            continue

        clip = clips.get(src.name)
        out.append(
            Candidate(
                candidate_id=src.stem,
                file=f"pool/{src.name}",
                shot_id=shot.shot_id,
                defects=list(clip.defects) if clip else [],
                severity=clip.severity if clip else "none",
                expected_verdict=clip.verdict if clip else None,
                provider=provider.name,
            )
        )

    return out


# ---------------------------------------------------------------------------
# 页面与静态资源
# ---------------------------------------------------------------------------

if config.WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(config.WEB_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    index_path = config.WEB_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(500, "web/index.html 缺失")
    return FileResponse(index_path)


@app.get("/media/{path:path}")
def media(path: str) -> FileResponse:
    """提供 data/ 下的媒体文件（候选视频、抽帧图）。做了路径穿越防护。"""
    root = config.DATA_DIR.resolve()
    target = (root / path).resolve()
    if not str(target).startswith(str(root)) or not target.is_file():
        raise HTTPException(404, "文件不存在")
    # 按扩展名给 Content-Type：以前一律写死 video/mp4，
    # 结果同目录下的 .jpg 抽帧图会被浏览器当成视频拒绝解码。
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type)


@app.get("/api/template")
def download_template(fmt: str = "xlsx") -> Any:
    """下载分镜表模板。

    * fmt=xlsx（默认）—— 直接用 Excel 打开改
    * fmt=csv  —— 纯文本，编码 UTF-8 BOM，Excel 也能正确显示中文
    """
    if not config.SHOTS_TEMPLATE.exists():
        raise HTTPException(404, "模板文件还没生成，请先运行 tools/make_dataset.py")

    if fmt.lower() == "csv":
        return FileResponse(
            config.SHOTS_TEMPLATE,
            media_type="text/csv",
            filename="分镜表模板.csv",
        )

    shots, _ = parse_table(config.SHOTS_TEMPLATE)
    stream = BytesIO()
    build_workbook(shots).save(stream)
    stream.seek(0)

    # 中文文件名要用 RFC 5987 编码，否则浏览器会显示成乱码
    filename = "分镜表模板.xlsx"
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": (
                "attachment; "
                'filename="shots_template.xlsx"; '
                f"filename*=UTF-8''{quote(filename)}"
            )
        },
    )


# ---------------------------------------------------------------------------
# 启动信息
# ---------------------------------------------------------------------------


@app.get("/api/bootstrap")
def bootstrap() -> dict[str, Any]:
    return {
        "project": runtime.project_view(),
        "results": [r.to_dict(include_evidence=False) for r in runtime.all_results()],
        "checks": describe_all(),
        "config": runtime.session.last_config,
        "pass_line": runtime.session.last_pass_line,
        "soft_tolerance": runtime.session.last_soft_tolerance,
        "session": runtime.session_view(),
        "run_summary": summarize(runtime.all_results()).to_dict(),
        "pool": _pool_view(),
    }


@app.get("/api/checks")
def list_checks() -> list[dict[str, Any]]:
    return describe_all()


@app.get("/api/progress")
def get_progress() -> dict[str, Any]:
    return progress.snapshot()


@app.get("/api/state")
def get_state() -> dict[str, Any]:
    return _state_response()


@app.get("/api/candidate/{shot_id}/{candidate_id}")
def candidate_detail(shot_id: str, candidate_id: str) -> dict[str, Any]:
    """单个候选的完整质检结果（含证据图）。"""
    shot = runtime.find(shot_id)
    result = runtime.cached_result(shot) if shot else None
    if result is None:
        raise HTTPException(404, "该分镜还没有质检结果")
    cand = next((c for c in result.candidates if c.candidate_id == candidate_id), None)
    if cand is None:
        raise HTTPException(404, "候选不存在")
    return cand.to_dict(include_evidence=True)


# ---------------------------------------------------------------------------
# 抽卡
# ---------------------------------------------------------------------------


@app.post("/api/draw")
def draw(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """抽卡：从资源池随机抽片段作为候选。

    * 传 shot_ids → 给这些分镜重新抽（替换已有候选）
    * 不传 shot_ids → 只给"还没抽过"的分镜抽
    * 不传 count → 按每个分镜自己的「候选数」抽（分镜表里配的），没配就是 4
    """
    shot_ids = payload.get("shot_ids") or None
    override = payload.get("count")
    override = int(override) if override else None

    if not load_pool():
        raise HTTPException(
            400,
            "视频资源池是空的。请先运行 tools/make_dataset.py 生成片段"
            "（或双击 start.bat 让它自动生成）。",
        )

    drawn: list[str] = []
    counts: dict[str, int] = {}
    for shot in runtime.require_project().shots:
        if shot_ids:
            if shot.shot_id not in shot_ids:
                continue
        elif shot.candidates:
            continue          # 不传 shot_ids 时，跳过已经抽过的

        shot.candidates = _draw_candidates(shot, _draw_count_for(shot, override))
        runtime.results.pop(shot.shot_id, None)   # 候选变了，旧结果自动失效
        runtime.session.clear_shot(shot.shot_id)
        drawn.append(shot.shot_id)
        counts[shot.shot_id] = len(shot.candidates)

    runtime.save()
    response = _state_response()
    response.update({"ok": True, "drawn": drawn, "counts": counts, "pool": _pool_view()})
    return response


# ---------------------------------------------------------------------------
# 质检
# ---------------------------------------------------------------------------


@app.post("/api/config")
def save_config(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """保存界面上的质检配置（配置一变，旧结果自动失效）。"""
    configs = [CheckConfig(**c) for c in (payload.get("configs") or [])]
    runtime.session.last_config = [c.to_dict() for c in configs]
    if "pass_line" in payload:
        runtime.session.last_pass_line = float(payload["pass_line"])
    if "soft_tolerance" in payload:
        runtime.session.last_soft_tolerance = float(payload["soft_tolerance"])
    runtime.save()
    return _state_response()


@app.post("/api/run")
def run_quality_check(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    configs = [CheckConfig(**c) for c in (payload.get("configs") or [])]
    pass_line = float(payload.get("pass_line", DEFAULT_PASS_LINE))
    tolerance = float(payload.get("soft_tolerance", DEFAULT_SOFT_TOLERANCE))
    shot_ids = payload.get("shot_ids") or None
    force = bool(payload.get("force", False))

    runtime.session.last_config = [c.to_dict() for c in configs]
    runtime.session.last_pass_line = pass_line
    runtime.session.last_soft_tolerance = tolerance
    runtime.save()

    active = parse_configs(configs)
    if not active:
        return _skip_response(
            "没有勾选任何检测项，无法质检。请点「打开质检配置」至少勾选一项。",
            no_checks=True,
        )

    targets = runtime.pending_shots(shot_ids, force=force)
    if not targets:
        message = (
            "这些分镜还没有抽卡，先点「抽卡」再来质检。"
            if not any(s.candidates for s in runtime.require_project().shots)
            else "没有需要质检的分镜（都已经质检过了）"
        )
        return _skip_response(message)

    Orchestrator(runtime).run_shots(targets, active, pass_line, tolerance)
    ran_ids = [s.shot_id for s in targets]

    response = _state_response()
    response.update(
        {
            "ran": len(ran_ids),
            "ran_shot_ids": ran_ids,
            "skipped": False,
            "shots": [
                r.to_dict(include_evidence=False)
                for r in runtime.all_results()
                if r.shot.shot_id in ran_ids
            ],
        }
    )
    return response


# ---------------------------------------------------------------------------
# 人工操作
# ---------------------------------------------------------------------------


@app.post("/api/adopt")
def adopt(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    shot_id = payload.get("shot_id")
    candidate_id = payload.get("candidate_id")
    if not shot_id or not candidate_id:
        raise HTTPException(400, "需要 shot_id 和 candidate_id")
    runtime.session.adopt(shot_id, candidate_id)
    runtime.save()
    return _state_response()


@app.post("/api/clear-adopt")
def clear_adopt(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    shot_id = payload.get("shot_id")
    if not shot_id:
        raise HTTPException(400, "需要 shot_id")
    runtime.session.clear_shot(shot_id)
    runtime.save()
    return _state_response()


@app.post("/api/adopt-top")
def adopt_top(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """一键采纳排名第一的候选——只从已经质检过的分镜里挑。"""
    shot_id = payload.get("shot_id")
    shot_ids = payload.get("shot_ids")

    adopted: dict[str, str] = {}
    skipped: list[str] = []

    for shot in runtime.require_project().shots:
        if shot_id and shot.shot_id != shot_id:
            continue
        if shot_ids and shot.shot_id not in shot_ids:
            continue

        result = runtime.cached_result(shot)
        if result is None:
            skipped.append(shot.shot_id)
            continue

        top = next((c for c in result.candidates if c.rank == 1), None)
        if top:
            runtime.session.adopt(shot.shot_id, top.candidate_id)
            adopted[shot.shot_id] = top.candidate_id
        else:
            skipped.append(shot.shot_id)

    runtime.save()
    response = _state_response()
    response.update({"adopted": adopted, "skipped_shots": skipped})
    return response


@app.post("/api/reset")
def reset() -> dict[str, Any]:
    runtime.session.reset()
    runtime.save()
    return _state_response()


# ---------------------------------------------------------------------------
# 分镜字段编辑（抽卡弹窗里改的提示词等）
# ---------------------------------------------------------------------------


@app.post("/api/shot")
def update_shot(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """更新一个分镜的可编辑字段。

    抽卡前会先弹窗让人确认 / 修改分镜信息（提示词、负面词、候选数、时长…），
    改完再抽。字段一旦改动，旧质检结果就不再代表这个分镜了，所以直接作废——
    反正紧接着就会重新抽卡，候选一变结果本来也会失效。
    """
    shot_id = payload.get("shot_id")
    if not shot_id:
        raise HTTPException(400, "需要 shot_id")
    shot = runtime.find(shot_id)
    if shot is None:
        raise HTTPException(404, f"分镜不存在：{shot_id}")

    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "提示词不能为空。")
    shot.prompt = prompt

    if "negative_prompt" in payload:
        shot.negative_prompt = (payload.get("negative_prompt") or "").strip()
    if "scene" in payload:
        shot.scene = (payload.get("scene") or "").strip()
    if "aspect_ratio" in payload:
        shot.aspect_ratio = (payload.get("aspect_ratio") or "").strip() or "16:9"
    if "candidates_per_shot" in payload:
        shot.candidates_per_shot = _clamp_int(
            payload.get("candidates_per_shot"), 1, 12, shot.candidates_per_shot
        )
    if "expected_duration_s" in payload:
        shot.expected_duration_s = _clamp_float(
            payload.get("expected_duration_s"), 0.5, 60.0, shot.expected_duration_s
        )
    for field in ("expected_faces", "expected_hands"):
        if field in payload:
            setattr(shot, field, _clamp_int(payload.get(field), 0, 8, None))

    runtime.results.pop(shot_id, None)
    runtime.save()
    response = _state_response()
    response.update({"ok": True, "shot": shot.to_dict()})
    return response


# ---------------------------------------------------------------------------
# 交付：导出采纳结果 + 合成预览片
# ---------------------------------------------------------------------------


@app.post("/api/export")
def export_selected() -> dict[str, Any]:
    """把已采纳的候选归集到 data/selected/，统一规格并写清单。"""
    clips, missing = _selected_or_400()
    try:
        result = deliver.export(clips)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
    result.update({"missing": missing})
    return result


@app.post("/api/preview")
def build_preview() -> dict[str, Any]:
    """导出 + 把所有采纳的片段拼成一条预览片。"""
    clips, missing = _selected_or_400()
    try:
        result = deliver.preview(clips)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
    result.update({"missing": missing})
    return result


def _selected_or_400() -> tuple[list, list[str]]:
    clips, missing = deliver.collect(runtime)
    if not clips:
        raise HTTPException(
            400, "还没有采纳任何分镜。先「一键采纳所有第一名」或手动采纳，再来交付。"
        )
    return clips, missing


# ---------------------------------------------------------------------------
# 重新抽卡 + 质检
# ---------------------------------------------------------------------------


@app.post("/api/reroll")
def reroll(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """对某个分镜重新抽卡，然后立刻质检这一个分镜。

    可选带上改过的 prompt / negative_prompt（诊断给出的建议）。
    """
    shot_id = payload.get("shot_id")
    if not shot_id:
        raise HTTPException(400, "需要 shot_id")

    shot = runtime.find(shot_id)
    if shot is None:
        raise HTTPException(404, f"分镜不存在：{shot_id}")

    if not load_pool():
        raise HTTPException(400, "视频资源池是空的，无法抽卡。")

    if payload.get("prompt"):
        shot.prompt = payload["prompt"]
    if "negative_prompt" in payload:
        shot.negative_prompt = payload.get("negative_prompt") or ""

    count = payload.get("count")
    shot.candidates = _draw_candidates(
        shot, _draw_count_for(shot, int(count) if count else None)
    )
    runtime.results.pop(shot_id, None)
    runtime.session.clear_shot(shot_id)
    round_no = runtime.session.bump_reroll(shot_id)
    runtime.save()

    configs = [CheckConfig(**c) for c in (payload.get("configs") or runtime.session.last_config)]
    active = parse_configs(configs)
    if not active:
        raise HTTPException(400, "没有勾选任何检测项，无法重抽质检。请先勾选至少一项。")
    pass_line = float(payload.get("pass_line", DEFAULT_PASS_LINE))
    tolerance = float(payload.get("soft_tolerance", DEFAULT_SOFT_TOLERANCE))

    result = Orchestrator(runtime).run_one(shot, active, pass_line, tolerance)

    response = _state_response()
    response.update(
        {
            "ok": True,
            "shot": result.to_dict(include_evidence=False),
            "reroll_round": round_no,
        }
    )
    return response


# ---------------------------------------------------------------------------
# 导入分镜表
# ---------------------------------------------------------------------------


@app.post("/api/import")
async def import_shots(file: UploadFile = File(...)) -> dict[str, Any]:
    if not file.filename:
        raise HTTPException(400, "没有文件名")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise HTTPException(400, "请上传 .csv 或 .xlsx 文件")

    # 上传的文件落在工作区内的临时目录，解析完就删——
    # 不用系统 %TEMP%：某些受限环境（沙箱/只读容器）写不进去。
    upload_dir = config.DATA_DIR / "_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = upload_dir / f"{uuid.uuid4().hex}{suffix}"

    try:
        with tmp_path.open("wb") as fh:
            shutil.copyfileobj(file.file, fh)
        project = import_csv(tmp_path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - Excel 损坏之类的
        raise HTTPException(400, f"无法解析这个文件：{exc}") from exc
    finally:
        tmp_path.unlink(missing_ok=True)

    _switch_project(project)
    response = _state_response()
    response.update({"ok": True, "checks": describe_all()})
    return response


@app.post("/api/init")
def initialize() -> dict[str, Any]:
    """初始化：清空当前项目，回到"刚打开程序"的状态。

    * 分镜、候选、质检结果、采纳记录、重抽次数 → 全部清空
    * 质检配置 → 恢复默认（总分线 / 容忍度 / 检测项勾选）
    * 视频资源池 → **不动**。它是"云端"，不是用户数据；初始化之后工作台是空的，
      想继续用示例分镜就点「下载分镜模板」再「导入分镜表」。

    前端在调用前会弹窗确认。
    """
    _switch_project(blank_project())
    runtime.session.last_config = []
    runtime.session.last_pass_line = DEFAULT_PASS_LINE
    runtime.session.last_soft_tolerance = DEFAULT_SOFT_TOLERANCE
    runtime.save()

    response = _state_response()
    response.update({"ok": True, "checks": describe_all()})
    return response


# ---------------------------------------------------------------------------


def _switch_project(project: Project) -> None:
    runtime.set_project(project, reset_results=True)
    runtime.session.reset()
    runtime.session.project_id = project.project_id
    runtime.save()


def _pool_view() -> dict[str, Any]:
    clips = load_pool()
    good = sum(1 for c in clips if c.is_good)
    return {"total": len(clips), "good": good, "bad": len(clips) - good}


def _skip_response(message: str, no_checks: bool = False) -> dict[str, Any]:
    response = _state_response()
    response.update(
        {
            "ran": 0,
            "skipped": True,
            "no_checks": no_checks,
            "message": message,
            "shots": [],
        }
    )
    return response


def _state_response() -> dict[str, Any]:
    """所有写操作都返回同一份最新状态，前端直接整块刷新。"""
    return {
        "project": runtime.project_view(),
        "session": runtime.session_view(),
        "run_summary": summarize(runtime.all_results()).to_dict(),
        "pool": _pool_view(),
    }


@app.exception_handler(Exception)
async def unhandled(_request, exc: Exception) -> JSONResponse:  # noqa: ANN001
    return JSONResponse(status_code=500, content={"error": str(exc)})
