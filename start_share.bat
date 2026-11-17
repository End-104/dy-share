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
rem   goods 997  = 1200  (same ruler as 1594 since 2026-11-17)
rem SHARE_DAY_CAP below is only the fallback when a cfg has no video_day_cap key.
set SHARE_DAY_CAP=100000
rem Both goods are share-count products; every window eats both, no idle windows.
set SHARE_GOODS=1594,997
rem Device gap 90s (was 300). The gate is device-wide, not per video: the
rem (device,video) ledger already blocks the whole natural day, so 300s only
rem delayed a device that just topped out on video A from moving to video B.
rem Pool ~8100 / 90s = 90 req/s ceiling. 30 windows x 180 credits at 35% accept
rem rate = ~67 req/s demand, so ~34% headroom. Never go below 60: at 60s a single
rem device serves 60 shares per hour and the device itself gets flagged.
set SHARE_DEVICE_GAP=90
set SHARE_ROUND_CHUNK=180
rem Log cadence: order-count line at most once per 30s, per-order skip notice per 60s.
set SHARE_LIST_LOG=30
set SHARE_SKIP_LOG=60
rem Zero-delta backoff base in seconds (150, capped at 3600).
set SHARE_ZERO_BACKOFF=150
rem Ceiling floor: a video already at/above this many credits today that yields a zero
rem delta in a full round gets parked to next day instead of retrying.
rem Left unset (=1000) it left videos stalled at 942 / 998 in a 3600s retry loop for good.
set SHARE_CEIL_MIN=900
rem Re-open interval after a sprint got interrupted by zero delta, in seconds.
set SHARE_PACE_HEAD_RETRY=120
rem No refund on this side: backoff and retry, carry the rest to next day.
rem Ceiling round length: send ~20s + stall judge 90s + pause 30s x 2 + settle 60s = ~230s.
rem Was 900 x 8, which always ran into SHARE_BUDGET and cost a flat 20 min per round.
set SHARE_STALL_PAUSE=30
set SHARE_STALL_ROUNDS=2
rem Pacing: 96h delivery window, sprint takes the whole remainder, head 10080 min.
set SHARE_PACE_HOURS=96
set SHARE_PACE_HEAD_PCT=1.0
set SHARE_PACE_HEAD_MIN=10080
rem One round at most 300s: a normal round needs ~25s (180 credits over 24 threads),
rem so this is a hard stop only for the drain-and-wait path, not a throughput cap.
set SHARE_BUDGET=300
rem Auto accept belongs to the resident daemon dy_share_intake.py, not the windows.
rem Each window only pulls its own page (page=argv[2], limit=24); 30 windows each
rem scanning one page of state=2 covers 1594 x22 + 997 x8 = 720 order slots, and a
rem window stuck inside the booster would accept with an hour of latency.
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
rem  goods 1594: 22 pages (cfg video_day_cap=1200) | goods 997: 8 pages (cfg 1200)
rem  30 windows x 24 threads = 720 concurrent share requests
rem  usage:
rem    start_share.bat         normal intake
rem    start_share.bat dry     dry run, no order state change (daemon too)
rem  knobs are the set lines above; per-goods cap comes from cfg video_day_cap.
rem ============================================================

rem Interpreter: prefer .venv312 (the name used in README), fall back to .venv.
rem This box only has .venv (Python 3.12.7); the old hardcoded .venv312 made every
rem window exit /b 1 while the daemon kept accepting orders.
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
rem small gap so the daemon grabs intake.lock before 30 windows race for orders
ping -n 3 127.0.0.1 >nul

rem ---- two goods groups, 15 windows each = 30 total ----
rem Page indexes must stay distinct across the whole fleet: the handler pulls
rem every goods on its own page number each round
rem   for _goods in GOODS_LIST: get_order_list('2', goods_sn=_goods)
rem so two windows on the same page index re-scan the same orders and only
rem fight over order_<sn>.lock. Hence 1594 takes pages 1-15, 997 takes 16-30:
rem 15 + 15 windows, 30 distinct pages, 30 x 24 = 720 order slots.
rem The cfg name on a window is just the default goods; supply comes from the
rem page index, which is why the second half starts at 16 instead of 1.
rem ---- goods 1594 : pages 1-15 ----
start "share-1594-p1" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1594 1 24 %MODE%"
start "share-1594-p2" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1594 2 24 %MODE%"
start "share-1594-p3" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1594 3 24 %MODE%"
start "share-1594-p4" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1594 4 24 %MODE%"
start "share-1594-p5" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1594 5 24 %MODE%"
start "share-1594-p6" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1594 6 24 %MODE%"
start "share-1594-p7" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1594 7 24 %MODE%"
start "share-1594-p8" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1594 8 24 %MODE%"
start "share-1594-p9" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1594 9 24 %MODE%"
start "share-1594-p10" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1594 10 24 %MODE%"
start "share-1594-p11" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1594 11 24 %MODE%"
start "share-1594-p12" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1594 12 24 %MODE%"
start "share-1594-p13" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1594 13 24 %MODE%"
start "share-1594-p14" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1594 14 24 %MODE%"
start "share-1594-p15" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1594 15 24 %MODE%"

rem ---- goods 997 : pages 16-30 (distinct indexes, same 15 windows) ----
start "share-997-p16" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_997 16 24 %MODE%"
start "share-997-p17" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_997 17 24 %MODE%"
start "share-997-p18" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_997 18 24 %MODE%"
start "share-997-p19" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_997 19 24 %MODE%"
start "share-997-p20" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_997 20 24 %MODE%"
start "share-997-p21" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_997 21 24 %MODE%"
start "share-997-p22" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_997 22 24 %MODE%"
start "share-997-p23" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_997 23 24 %MODE%"
start "share-997-p24" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_997 24 24 %MODE%"
start "share-997-p25" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_997 25 24 %MODE%"
start "share-997-p26" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_997 26 24 %MODE%"
start "share-997-p27" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_997 27 24 %MODE%"
start "share-997-p28" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_997 28 24 %MODE%"
start "share-997-p29" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_997 29 24 %MODE%"
start "share-997-p30" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_997 30 24 %MODE%"

echo 30 intake windows launched: 1594 x15 (pages 1-15) + 997 x15 (pages 16-30), 24 threads each = 720 slots.
echo 1 state daemon launched: share-intake (accept 1-^>2 / revive 3-^>2 / finish 2-^>4), %SHARE_INTAKE_CYCLE%s per round.
echo per-goods video day cap comes from cfg video_day_cap; fallback SHARE_DAY_CAP=%SHARE_DAY_CAP%, device gap %SHARE_DEVICE_GAP%s, chunk %SHARE_ROUND_CHUNK%.
