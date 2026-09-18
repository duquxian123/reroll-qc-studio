"""路径与常量。所有产物都落在工作区内。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = ROOT / "data"
# 视频资源池：所有片段平铺在这里，文件名自带状态（OK / 缺陷-严重度）
POOL_DIR = DATA_DIR / "pool"
POOL_LABELS = POOL_DIR / "labels.json"
# 内置分镜表（也是「下载分镜模板」的下载内容）
SHOTS_TEMPLATE = DATA_DIR / "shots_template.csv"
# 公开的人脸 / 手部素材
ASSET_DIR = DATA_DIR / "face_assets"
# 交付产物：采纳的片段 + 清单 + 预览片（每次导出都会重建）
SELECTED_DIR = DATA_DIR / "selected"

WEB_DIR = ROOT / "web"
STATE_DIR = ROOT / "state"
SESSION_PATH = STATE_DIR / "session.json"
# 当前项目（分镜表 + 抽到的候选）。删掉它 = 下次启动回到内置示例。
PROJECT_PATH = STATE_DIR / "project.json"

HOST = "127.0.0.1"
PORT = 8756
