@echo off
rem ============================================================
rem  FishingBot 启动器
rem  1) 申请一次管理员权限（游戏前台独占输入时需要）
rem  2) 启动 Web 控制台 (webui.py)
rem  3) 启动游戏上方悬浮信息条 (overlay.py)
rem ============================================================
setlocal
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo 正在申请管理员权限，请在弹窗中点"是"...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
title FishingBot
cd /d "%~dp0"

rem --- 1) Web 控制台（--no-elevate 因为已经提过权了）---
start "FishingBot WebUI" /min "" "C:\Users\杨\.workbuddy\binaries\python\envs\default\Scripts\python.exe" "%~dp0webui.py" --no-elevate

rem --- 2) 等 webui 起来再启动悬浮条 ---
timeout /t 3 /nobreak >nul
start "FishingBot Overlay" "" "C:\Users\杨\.workbuddy\binaries\python\envs\default\Scripts\python.exe" "%~dp0overlay.py"

echo.
echo   已启动: Web 控制台 + 悬浮信息条
echo.
echo   悬浮条操作:
echo     拖动        - 先右键切到控制模式再拖（信息模式鼠标穿透拖不动）
echo     右键        - 切换 信息模式 / 控制模式
echo     Ctrl+Alt+O  - 同上（全局热键）
echo     Ctrl+Alt+R  - 录制 10 秒参考音效（全局热键）
echo     Ctrl+Alt+T  - 3 秒后按下动作键（全局热键）
echo     Ctrl+Alt+X  - 退出悬浮条
echo.
echo   本窗口 8 秒后自动关闭，Web 控制台与悬浮条会继续运行。
timeout /t 8 /nobreak >nul
exit /b 0
