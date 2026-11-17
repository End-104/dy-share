@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
rem ---- 交付口径：客户下多少刷多少，今天不齐明天接着刷 ----
rem day cap 抬到 10 万 = 本地不设限（平台真实天花板 341~639/天仍由 delta 归零体现）
set SHARE_DAY_CAP=100000
rem both goods are share-count products; every window eats both, no idle windows
set SHARE_GOODS=1763,643
set SHARE_DEVICE_GAP=300
set SHARE_ROUND_CHUNK=180
rem log cadence: order-count line at most once per 30s, per-order skip notices once per 60s
set SHARE_LIST_LOG=30
set SHARE_SKIP_LOG=60
rem 零增量退避基数（秒）：600 太钝，单子一天里大半时间在睡；压到 150，封顶仍是 3600
set SHARE_ZERO_BACKOFF=150
rem 冲刺被零增量打断后的重开间隔（秒）：600 -> 120，关窗前多试几轮
set SHARE_PACE_HEAD_RETRY=120
rem 退款链路与播放端一致：本端不发退款请求、不写状态 7，只退避重试 + 跨天待刷
set SHARE_STALL_PAUSE=900
set SHARE_STALL_ROUNDS=8
rem ---- 节奏层：整单 96 小时交付期，冲刺段吃满全单（1.0），头段 10080 分钟 ----
set SHARE_PACE_HOURS=96
set SHARE_PACE_HEAD_PCT=1.0
set SHARE_PACE_HEAD_MIN=10080
rem 一轮最长 1200 秒：撞桶关窗就退出，让别的窗口拿设备
set SHARE_BUDGET=1200
rem ---- 自动接单交给常驻守护进程 dy_share_intake.py ----
rem 窗口内接单必须关掉：每个窗口的 get_order_list 只用自己那页(page=argv[2], limit=24)，
rem 12 个窗口各扫已付款的一页，超过 24 张就漏单，且窗口卡在 booster 里接单延迟会到一小时
set SHARE_AUTO_ACCEPT=0
set SHARE_INTAKE_MAX=30
rem 守护进程：20 秒一圈，每页 50 条最多 20 页，单次 HTTP 15 秒超时（不继承 function.py 的死循环）
set SHARE_INTAKE_CYCLE=20
set SHARE_INTAKE_PAGE_LIMIT=50
set SHARE_INTAKE_PAGES=20
set SHARE_INTAKE_TIMEOUT=15
rem 完成判定缺量 5 条以内即置已完成，与 handler 的 LOSE_NUM 同口径
set SHARE_LOSE_NUM=5
rem 异常单最多回收 3 次；写 4 之后回读复核 3 次，间隔 2 秒（平台写库有延迟）
set SHARE_REVIVE_MAX=3
set SHARE_CONFIRM_RETRY=3
set SHARE_CONFIRM_GAP=2

rem ============================================================
rem  Share-count intake end (own venv: .venv312)
rem  goods 1763: 6 pages | goods 643: 6 pages | 24 threads per order
rem  usage:
rem    start.bat            normal intake
rem    start.bat dry        dry run, no order state change
rem  knobs:
rem    SHARE_DAY_CAP=100000    max credited per video per natural day (local ceiling, off by default)
rem    SHARE_PACE_HOURS=96     whole-order delivery window, keeps cross-day segments full length
rem    SHARE_PACE_HEAD_PCT=1.0 sprint covers the full remainder, no curve/hourly gate
rem    SHARE_AUTO_ACCEPT=0     window-level auto accept off, the intake daemon owns it
rem    SHARE_INTAKE_CYCLE=20   intake daemon round interval in seconds
rem    SHARE_REVIVE_MAX=3      abnormal(3) -> processing(2) attempts per order
rem    SHARE_INTAKE_MAX=30     max orders promoted per window round (only used when SHARE_AUTO_ACCEPT=1)
rem    refund: none on this side; stuck orders keep retrying and roll to next day
rem    SHARE_DEVICE_GAP=300    min seconds between shares from same device
rem    SHARE_ROUND_CHUNK=180   max items per round, then back to queue tail
rem    SHARE_STALL_PAUSE=900   seconds to pause while waiting for counter settle
rem    SHARE_STALL_ROUNDS=8    pause rounds before giving up
rem ============================================================

rem 解释器自适应：优先 .venv312（README 里的规范名），没有就退回 .venv。
rem 原来写死 .venv312，而这台机器上只有 .venv，脚本直接 exit /b 1，12 个窗口一个都起不来，
rem 订单却已经在平台上自动接单（守护进程另起），看起来就是「没人刷、单子自己在走」。
set PY=%~dp0.venv312\Scripts\python.exe
if not exist "%PY%" set PY=%~dp0.venv\Scripts\python.exe
if not exist "%PY%" (
    echo [ERROR] python not found: %PY%
    echo rebuild: py -3.12 -m venv .venv312
    echo deps   : .venv312\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

set MODE=%1
rem dry 模式要一起传下去，否则窗口干跑而守护进程真写平台状态
if /i "%MODE%"=="dry" set SHARE_INTAKE_DRY=1

rem ---- 接单 / 回收 / 完成守护：先起它，窗口起来之前已付款的单就已经进了处理中 ----
start "share-intake" cmd /k "chcp 65001>nul && cd /d %~dp0 && start_intake.bat"

rem ---- goods 1763 ----
start "share-1763-p1" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1763 1 24 %MODE%"
start "share-1763-p2" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1763 2 24 %MODE%"
start "share-1763-p3" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1763 3 24 %MODE%"
start "share-1763-p4" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1763 4 24 %MODE%"
start "share-1763-p5" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1763 5 24 %MODE%"
start "share-1763-p6" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_1763 6 24 %MODE%"

rem ---- goods 643 ----
start "share-643-p1" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_643 1 24 %MODE%"
start "share-643-p2" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_643 2 24 %MODE%"
start "share-643-p3" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_643 3 24 %MODE%"
start "share-643-p4" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_643 4 24 %MODE%"
start "share-643-p5" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_643 5 24 %MODE%"
start "share-643-p6" cmd /k "chcp 65001>nul && cd /d %~dp0 && %PY% ayk_dy_share_handler.py cfg_qw_dy_share_643 6 24 %MODE%"

echo 12 intake windows launched: 1763 x6 pages, 643 x6 pages, 24 threads each.
echo 1 state daemon launched: share-intake (accept 1-^>2 / revive 3-^>2 / finish 2-^>4), %SHARE_INTAKE_CYCLE%s per round.
echo day cap %SHARE_DAY_CAP% per video, device gap %SHARE_DEVICE_GAP%s, chunk %SHARE_ROUND_CHUNK%.
