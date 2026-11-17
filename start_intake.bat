@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
if not defined SHARE_INTAKE_CYCLE set SHARE_INTAKE_CYCLE=20
title share-intake

rem 解释器自适应：优先 .venv312，没有就退回 .venv（这台机器实际只有 .venv）
set PY=.venv312\Scripts\python.exe
if not exist "%PY%" set PY=.venv\Scripts\python.exe

rem 守护进程死了要自己爬起来：窗口重启一次要人工盯，订单不能没人接单
:loop
"%PY%" dy_share_intake.py
set RC=%ERRORLEVEL%
if "%RC%"=="3" (
    echo [intake] 已有实例持有 intake.lock，本窗口退出
    ping -n 6 127.0.0.1 >nul
    exit /b 3
)
echo [intake] 退出码 %RC%，5 秒后重启
ping -n 6 127.0.0.1 >nul
goto loop
