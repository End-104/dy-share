@echo off
chcp 65001 >nul
setlocal
cd /d D:\dy_share

rem ============================================================
rem  run_1800.bat - single-video daily share-count producer
rem
rem  why these numbers (all measured, not guessed):
rem   - only fresh identities credit: old pool 0%, on-the-fly 44%
rem   - device registration is windowed: ~0.93/s sustained, bursts get blocked
rem   - a (device x video) pair credits once, that pair is the hard key
rem   - 1800 / 44% ~= 4090 fresh identities, mint while burning
rem   - channel buckets are counted separately: 1128 / 32
rem
rem  usage:
rem     run_1800.bat                        use TARGET below
rem     run_1800.bat 7693857502727274422    explicit video id
rem ============================================================

set "TARGET=%~1"
if "%TARGET%"=="" set "TARGET=7693857502727274422"

set PYTHONIOENCODING=utf-8
set SHARE_DAY_CAP=1800
set SHARE_RANDOM_EXTRA=0

echo [start] %DATE% %TIME% target %TARGET% cap 1800
echo [start] pools and ledger:
"D:\dy_share\.venv312\Scripts\python.exe" -X utf8 -c "import sys,os; sys.path.insert(0,r'D:\dy_share'); os.chdir(r'D:\dy_share'); import dy_share as D, used_store as U; print('  devices', len(D.devices34), '+ minted', len(D.load_extra_devices()), '| infos', len(D.infos), '+', len(D.load_extra_infos())); print('  ledger', U.video_day_report())"

rem args:
rem   --threads 24      concurrent reporting threads
rem   --chunk 180       max items per window (measured capacity ~220)
rem   --gap 240         first backoff after window closes (4 min)
rem   --gap-max 1800    backoff ceiling (30 min)
rem   --mint-batch 400  mint 400 devices per batch (~7 min)
rem   --mint-threads 2 --mint-pace 1.0   measured 0.93/s, 96% success
rem   --stop-at 23:30   stop time
"D:\dy_share\.venv312\Scripts\python.exe" -X utf8 daily1800.py %TARGET% 1800 ^
  --threads 24 --chunk 180 --gap 240 --gap-max 1800 --stop-at 23:30 ^
  --mint-batch 400 --mint-threads 2 --mint-pace 1.0 ^
  >> daily_stdout.log 2>&1

echo [done] %DATE% %TIME%
echo [done] detail: daily_run.jsonl | state: daily_state.json | console: daily_stdout.log
endlocal
