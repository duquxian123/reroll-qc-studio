@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "ROOT=%~dp0"

rem --- 让 uv / Python 的所有产物都落在工作区内，系统零污染 ---
set "UV_CACHE_DIR=%ROOT%.uv-cache"
set "UV_PYTHON_INSTALL_DIR=%ROOT%.python"
set "UV_PROJECT_ENVIRONMENT=%ROOT%.venv"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
rem matplotlib 是 mediapipe 的依赖，默认往 AppData 写缓存，这里也挪进工作区
set "MPLCONFIGDIR=%ROOT%.uv-cache\mpl"

rem --- 定位 uv ---
set "UV=uv"
where uv >nul 2>nul
if errorlevel 1 set "UV=%LOCALAPPDATA%\Programs\uv\uv.exe"
if not exist "%UV%" if /i not "%UV%"=="uv" (
    echo.
    echo [错误] 找不到 uv。请先安装 uv：https://docs.astral.sh/uv/
    echo.
    pause
    exit /b 1
)

rem --- 首次运行：准备 Python 环境 ---
if not exist "%ROOT%.venv\Scripts\python.exe" (
    echo.
    echo [1/3] 首次运行，正在准备工作区内的 Python 环境，约需 1-3 分钟...
    echo.
    "%UV%" sync || goto :fail
)

rem --- 首次运行：生成视频资源池 ---
if not exist "%ROOT%data\pool\labels.json" (
    echo.
    echo [2/3] 正在生成视频资源池（约 1 分钟）...
    echo.
    "%ROOT%.venv\Scripts\python.exe" "%ROOT%tools\make_dataset.py" || goto :fail
)

echo.
echo [3/3] 启动服务中，浏览器即将自动打开： http://127.0.0.1:8756
echo       关闭这个窗口即可停止服务。
echo.
start "" "http://127.0.0.1:8756"
"%ROOT%.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8756
goto :eof

:fail
echo.
echo [错误] 启动失败。请把上面的报错信息发给我。
echo.
pause
exit /b 1
