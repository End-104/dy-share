@echo off
rem 计划任务入口：无控制台环境下启动监督循环，输出全部落到 intake.log
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0start_intake.bat" >> "%~dp0intake.log" 2>&1
