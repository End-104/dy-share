@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Total entry for share-count intake. Same as start_share.bat
call "%~dp0start_share.bat" %*
