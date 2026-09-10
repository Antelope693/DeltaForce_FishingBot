@echo off
rem ============================================================
rem  钓鱼挂机 Web 控制台 - 启动器
rem  说明: 脚本内部会自动申请管理员权限, 弹出 UAC 时请点 是
rem ============================================================
title Fishing Bot WebUI
"C:\Users\杨\.workbuddy\binaries\python\envs\default\Scripts\python.exe" "%~dp0webui.py"
echo.
echo 控制台已退出。
pause
