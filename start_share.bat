@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
rem ============================================================
rem  THIS FILE MUST STAY PURE ASCII.
rem  cmd.exe parses .bat in the ANSI code page (936/GBK on this box).
rem  A UTF-8 Chinese comment is 3 bytes per char; GBK eats them 2 at a time,
rem  the odd leftover byte swallows the CR and the first letters of the next
rem  line, so "rem ..." lines get executed as commands and "set" lines break.
rem  Chinese notes live in README_share.md instead.
rem ============================================================
rem Delivery policy: fulfill what the customer ordered; spill over to next day.
rem Per-goods daily cap now lives in cfg_qw_dy_share_<goods>.json -> video_day_cap:
rem   goods 1594 = 1200  (max 1200 credited per video per natural day)
rem   goods 997  = 0     (unlimited, every cap check inside the booster is skipped)
rem SHARE_DAY_CAP below is only the fallback when a cfg has no video_day_cap key.
set SHARE_DAY_CAP=100000
rem Both goods are share-count products; every window eats both, no idle windows.
set SHARE_GOODS=1594,997
set SHARE_DEVICE_GAP=300
set SHARE_ROUND_CHUNK=180
rem Log cadence: order-count line at most once per 30s, per-order skip notice per 60s.
set SHARE_LIST_LOG=30
set SHARE_SKIP_LOG=60
rem Zero-delta backoff base in seconds (150, capped at 3600).
set SHARE_ZERO_BACKOFF=150
rem Re-open interval after a sprint got interrupted by zero delta, in seconds.
set SHARE_PACE_HEAD_RETRY=120
rem No refund on this side: backoff and retry, carry the rest to next day.
set SHARE_STALL_PAUSE=900
set SHARE_STALL_ROUNDS=8
rem Pacing: 96h delivery window, sprint takes the whole remainder, head 10080 min.
set SHARE_PACE_HOURS=96
set SHARE_PACE_HEAD_PCT=1.0
set SHARE_PACE_HEAD_MIN=10080
rem One round at most 1200s: close the window on bucket cap and free the devices.
set SHARE_BUDGET=1200
rem Auto accept belongs to the resident daemon dy_share_intake.py, not the windows.
rem Each window only pulls its own page (page=argv[2], limit=24); 12 windows each
rem scanning one page of state=2 leaks orders past 24, and a window stuck inside
rem the booster would accept with an hour of latency.
set SHARE_AUTO_ACCEPT=0
set SHARE_INTAKE_MAX=30
rem Daemon: 20s per round, 50 per page up to 20 pages, 15s single HTTP timeout.
set SHARE_INTAKE_CYCLE=20
set SHARE_INTAKE_PAGE_LIMIT=50
set SHARE_INTAKE_PAGES=20
set SHARE_INTAKE_TIMEOUT=15
rem Shortfall within 5 counts as done, same ruler as LOSE_NUM in the handler.
set SHARE_LOSE_NUM=5
rem Recycle an abnormal order (state 3) at most 3 times, then read back 3x / 2s.
set SHARE_REVIVE_MAX=3
set SHARE_CONFIRM_RETRY=3
set SHARE_CONFIRM_GAP=2

rem ============================================================
rem  Share-count intake end (own venv: .venv312)
rem  goods 1594: 6 pages (cfg video_day_cap=1200) | goods 997: 6 pages (no cap)
rem  24 threads per order
rem  usage:
rem    start_share.bat         normal intake
rem    start_share.bat dry     dry run, no order state change (daemon too)
rem  knobs are the set lines above; per-goods cap comes from cfg video_day_cap.
rem ============================================================

rem Interpreter: prefer .venv312 (the name used in README), fall back to .venv.
rem This box only has .venv (Python 3.12.7); the old hardcoded .venv312 made all
rem 12 windows exit /b 1 while the daemon kept accepting orders.
set "PY=%~dp0.venv312\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] python not found: %PY%
    echo rebuild: py -3.12 -m venv .venv312
    echo deps   : .venv312\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)
echo [launcher] python = %PY%

set "MODE=%~1"
rem Pass dry down, otherwise windows dry-run while the daemon really writes state.
if /i "%MODE%"=="dry" set "SHARE_INTAKE_DRY=1"

rem ---- state daemon first: orders paid before the windows start are already work ----
start "share-intake" cmd /k "chcp 65001>nul && cd /d %~dp0 && start_intake.bat"
rem small gap so the daemon grabs intake.lock before 12 windows race for orders
ping -n 3 127.0.0.1 >nul

rem ---- goods 1594 (cfg video_day_cap=1200) ----
start "share-1594-p1" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1594 1 24 %MODE%"
start "share-1594-p2" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1594 2 24 %MODE%"
start "share-1594-p3" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1594 3 24 %MODE%"
start "share-1594-p4" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1594 4 24 %MODE%"
start "share-1594-p5" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1594 5 24 %MODE%"
start "share-1594-p6" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1594 6 24 %MODE%"

rem ---- goods 997 (cfg video_day_cap=0, unlimited) ----
start "share-997-p1" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_997 1 24 %MODE%"
start "share-997-p2" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_997 2 24 %MODE%"
start "share-997-p3" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_997 3 24 %MODE%"
start "share-997-p4" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_997 4 24 %MODE%"
start "share-997-p5" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_997 5 24 %MODE%"
start "share-997-p6" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_997 6 24 %MODE%"

echo 12 intake windows launched: 1594 x6 pages (cap 1200/video/day), 997 x6 pages (no cap), 24 threads each.
echo 1 state daemon launched: share-intake (accept 1-^>2 / revive 3-^>2 / finish 2-^>4), %SHARE_INTAKE_CYCLE%s per round.
echo per-goods video day cap comes from cfg video_day_cap; fallback SHARE_DAY_CAP=%SHARE_DAY_CAP%, device gap %SHARE_DEVICE_GAP%s, chunk %SHARE_ROUND_CHUNK%.
