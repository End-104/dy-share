# coding: utf-8
"""ayk_dy_share_handler.py —— 供应商分享量订单处理端（货号 997 / 1594）

与播放端 ayk_dy_bf_handler.py 完全同构，只把「播放量」换成「分享量」：
    播放端：get_play_count + ShareBooster(field='',  counter=get_play_count)
    分享端：share_count    + ShareBooster(field='share_delta', counter=share_count)

用法：
    python ayk_dy_share_handler.py cfg_qw_dy_share_1594 1 24         # 正常接单
    python ayk_dy_share_handler.py cfg_qw_dy_share_1594 1 24 dry     # 演练：只试打，不改订单
    python ayk_dy_share_handler.py cfg_qw_dy_share_997  1 24

第三个参数是每单线程数（省略=20），第四个参数是 dry 时进演练模式。
单个进程只处理一个货号的一页订单。货号 997 与 1594 各起若干进程，
页码错开，靠平台 execState 锁避免两个进程抢同一单。

每单流程（10 步，输入 -> 输出）：
    1  拉单       get_order_list('2')                      -> state=2 的订单数组
    2  读详情     get_order_detail(order_sn)               -> 订单对象，校验 state 仍为 2
    3  抢锁       execState?order_sn=..                    -> '1' 表示已被别的进程领走，跳过
    4  换 ID      execute?order_sn=..&exec_update=1         -> 19 位 aweme_id，失败置 state=3
    5  查额度     video_room(aweme_id, day_cap)             -> 今日剩余条数；<=5 直接把单排到次日零点
    6  读基数     share_count(aweme_id)                    -> 当前真实分享量；读不到回 -1，本轮不记账不判定
    7  建台账     order_settle.plan(单号, 下单量, 货号)      -> 分几天、今天切片还能吃多少
    8  算缺量     min(整单缺口, 今日额度, 今日切片, 单轮)     -> 本轮目标
    9  上报       ShareBooster.run(day_cap=...)            -> accepted/delta/day_capped
    10 结算       再读 share_count -> update_order_progress -> 完成(4) / 退款(7) / 明日续刷

交付与退款口径（2026-11-24 起）：
    下单量 <= 1200：单日单。只在接单当日交付，当天收工即结算，
                   退款 = 下单量 - 当日官方净增，缺多少退多少，不跨天、不补刷。
    下单量 >  1200：多日单。天数 = ceil(下单量 / 1200) 接单时算定，
                   前 N-1 天各吃一片（1200），只有第 N 天收工才结算，
                   退款 = 下单量 - 累计官方净增。中间天吃不满也不顺延、不回补。
    例外：视频不可读（分享量连续 6 次读不到）→ 任何一天立即按已交付量结算。
          6 次而不是 3 次：share_count 有 600 秒负缓存，结算扫描 60 秒一轮，
          3 次只要 3 分钟就能在一次接口抖动里攒满，等于拿一次网络抖动去退款。
    退款基数：一律取「官方 share_count - 本单本地基线」，不取平台 startNum /
          currentNum / finishTotal / execNum。平台这几个字段实测恒小于真实分享量
          （12 条抽样里 4 条少 11~55 条），按平台退就是少退客户。
    结算时刻：末天 22:30（SHARE_SETTLE_AFTER）；停机错过则次日扫描补结。
    退款只报条数（/api/supplier/order/v2/refund 的 number），钱由平台按原路退，
    本端不碰商户号与支付通道；幂等靠 settle/s_<单号>.json + settle/.lock_<单号>。

单视频每日口径（跨进程共享，台账在 used_devices.db，按货号取值）：
    货号 1594：同一条视频每个自然日最多入账 1200 条（cfg 里 "video_day_cap": 1200）；
    货号 997 ：不限（cfg 里 "video_day_cap": 0，video_room 回 10^9、day_cap_hit 恒 False）；
    配置里没写 video_day_cap 的退回 SHARE_DAY_CAP 环境变量（默认 10 万，等于不设限）。
    第二天零点自动重置，同一条视频按当天口径重新计额度，不需要人工清库。
    切片基数等于这个日限；日限为 0（不限）时切片按 SHARE_SLICE_CAP=1200 算，
    否则多日单会算成「一天刷完」，切片形同不存在。

多单并发时的调度口径（保证每个用户都能吃满自己的额度）：
    · SHARE_ROUND_CHUNK=180  每轮只吃 180 条就回队尾，避免前几单占满设备
    · SHARE_DEVICE_GAP=90    同一台设备两次分享最小间隔（代码缺省 300，launcher 设 90）。
                             设备池实测 7941 台 ÷ 90s = 88 请求/秒上限，30 个窗口
                             × 180 条 ÷ 230s 一轮 ÷ 计入率 35% ≈ 67 请求/秒需求，尚有余量。
    · SHARE_ZERO_BACKOFF=150 零增量第一档退避（原来 600）。退避单不占窗口：跳过即走，
                             下一轮再来，窗口立刻去处理别的单。
    · SHARE_PAGE_MAX=30      页池大小：页不再绑死在窗口上，谁闲谁领，干完让出来。
    · SHARE_PAGE_SWEEP=6     一轮最多连领几页，全空才认「池里现在没活」。
    · SHARE_PREFETCH=1       预取线程：跑当前单时把下一单的详情/占位/额度先取回来，
                             换手从 8~20 秒降到 1 秒内。
    · SHARE_IDLE_SLEEP=15    整池都没活时的整觉（秒），封顶就是一个 IDLE_SLEEP。
"""

import collections
import gc
import json
import os
import sys
import threading
import time

from dy_share import (DevicePool, InfoPool, ShareBooster, SHARE_FIELD,
                      VIDEO_DAY_CAP, devices34, infos, load_extra_devices,
                      load_extra_infos, resolve_aweme_id, share_count, video_state)
from function import FunctionApp, show_message, TIME_WAIT, http_client
import used_store as U
import pace_ledger as P
import order_baseline as B
import order_settle as S

function = FunctionApp()

CODE_VERSION = '2026-11-24.1'   # 每次改动代码都要动这个号，窗口一开就能看出跑的是哪一份
LOSE_NUM = 5                # 余量：分享按真实增量结算，留 5 条收口，留大等于白刷
THREADS = int(sys.argv[3]) if len(sys.argv) > 3 and str(sys.argv[3]).isdigit() else 20
# 每单每轮最长耗时（秒）。原来是 2400，给「1800 条满额单一次吃完」留的余量；
# 现在节奏层不再限速，一轮吃到视频桶关窗或吃满本批 180 条就自然退出，
# 2400 秒只会让一条单占住一个窗口四十分钟，其余 29 个窗口排在同一单后面干等。
BUDGET = int(os.environ.get('SHARE_BUDGET', 60 * 20))
CHANNELS = ('1128', '32')   # 实测能承载 share_delta 的两条 Android 通道
# ---- 交付与退款口径（2026-11-24 起，取代旧的「不退单、缺量挂次日」）
#   total <= 1200：单日单，只在接单当日交付，当天收工就结算，缺多少退多少，不跨天。
#   total >  1200：多日单，days = ceil(total/1200) 接单时算定，前 days-1 天各吃一个切片，
#                  只有末天（第 days 天）收工才结算，退款 = total - 累计官方净增。
#   例外：视频不可读（统计连续读不到）→ 任何一天立即结算。
# 结算时刻：末天 22:30（SHARE_SETTLE_AFTER 可调）之后；下线停机错过的单次日补结。
# 退款走 /api/supplier/order/v2/refund 报条数，钱由平台按原路/余额退；
# 本端只报数量，不碰商户号与支付通道。幂等靠 settle/s_<单号>.json 的
# refund_reported 标记 + settle/.lock_<单号> 互斥，重启和并发都不会重报。
# zero_rounds 退避链保留：它只负责「关窗时别烧设备」，与结算无关。
ZERO_BACKOFF = float(os.environ.get('SHARE_ZERO_BACKOFF', 150))   # 零增量退避基数（秒），每轮翻倍，封顶 3600
# 结算扫描每圈最多处理几条到期单：一次扫太多会把拉单拖慢，
# 而到期单本来就不急（已经收工），分几圈结完完全可以。
SETTLE_BATCH = int(os.environ.get('SHARE_SETTLE_BATCH', 5))
# 结算扫描间隔（秒）：30 个窗口每圈都扫一遍 settle/ 目录纯属浪费 syscall，
# 到期单不急（已经收工），两分钟扫一次足够，也保证零点后很快补结。
SETTLE_EVERY = float(os.environ.get('SHARE_SETTLE_EVERY', 120))

# ---- 平台天花板：本地台账的数字不是平台额度，实测天花板 341~639 的视频打到顶之后
# delta 会长时间归零，这不是死号也不是限流，是这条视频当天的记账预算用完了。
# 判到天花板只把单子挪到次日零点，不计数、不退单。
# CEIL_MIN_TODAY：今天至少吃到这么多条才允许判「当日天花板」并推到零点。
# 日限抬到 10 万以后，写死的 1000 会让「今天进过 1000 条、这一轮恰好零增量」的
# 普通关窗被误判成日顶，一判就跳过一整天。改成跟着货号日限走：日限 - 800。
# 货号 1594 日限 1200 算出来 1000；日限仍是 1800 的老配置算出来还是 1000（行为不变）；
# 货号 997 日限 0（不限）没有本地日顶可言，判据给一个够不到的数，
# 零增量一律落到退避那条分支，不会被误推到次日零点白等一整天。
CEIL_MIN_ENV = int(os.environ.get('SHARE_CEIL_MIN', 0))


def ceil_min_today(day_cap: int) -> int:
    """某货号的「当日天花板」判定门槛：今天至少进这么多条，才允许判平台日顶。"""
    if CEIL_MIN_ENV > 0:
        return CEIL_MIN_ENV
    cap = int(day_cap)
    if cap <= 0:
        return 10 ** 9
    return max(1000, cap - 800)


def cap_text(day_cap: int) -> str:
    """日限的人话写法：0 打成「不限」，免得日志里出现「/0」被当成除零 bug。"""
    return '不限' if int(day_cap) <= 0 else str(int(day_cap))

# ---- 订单级互斥：一条单子同一时刻只允许一个窗口发上报
# 三方接口的 execState 不是会话语义：execute 拿回来的 1 只代表「这一次领取」，
# 下一秒再查又回 2。30 个窗口因此会同时对着同一条单子各发一批，实测把 800 条
# 的单子刷到 1100（超交 300，等于白送）。平台侧没有可用的订单锁，只能在本机做：
# 谁抢到 order_<sn>.lock 谁刷，抢不到的直接跳过，等下一圈再抢。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ORDER_LOCK_TTL = float(os.environ.get('SHARE_ORDER_LOCK_TTL', BUDGET + 600))
# 视频归属多久没动静可以收回。定 1 小时：单条视频的日额度 1800 条，正常一单
# 几十分钟内必然有回写；超过 1 小时没有任何一轮碰它，说明持单窗口已经不在了。
VIDEO_OWNER_TTL = float(os.environ.get('SHARE_VIDEO_OWNER_TTL', 3600))


def _lock_path(order_sn):
    return os.path.join(BASE_DIR, 'order_{}.lock'.format(order_sn))


def _pid_alive(pid):
    """同机判活：进程没了就是死锁，不用等 TTL 白等到 50 分钟。

    注意：Windows 上 os.kill(pid, 0) 不是「空探测」，CPython 会走到
    TerminateProcess —— 用它判活等于把持锁窗口直接杀掉（实测 24 个窗口
    互杀剩 20 个，锁从此没人放）。所以 Windows 走 OpenProcess + WaitForSingleObject。
    """
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except Exception:
            return True
    try:
        import ctypes
        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x102
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.WaitForSingleObject.restype = ctypes.c_uint32
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(SYNCHRONIZE, 0, pid)
        if not h:
            return False
        try:
            return k32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
        finally:
            k32.CloseHandle(h)
    except Exception:
        return True


def claim_order(order_sn):
    """抢订单锁。拿到 True 才允许发上报，False 说明别的窗口正在刷。"""
    path = _lock_path(order_sn)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, ('%d %.0f\n' % (os.getpid(), time.time())).encode())
            os.close(fd)
            return True
        except FileExistsError:
            holder, ts = '', 0.0
            try:
                with open(path, encoding='utf-8') as f:
                    parts = f.read().split()
                holder = parts[0] if parts else ''
                ts = float(parts[1]) if len(parts) > 1 else os.path.getmtime(path)
            except Exception:
                ts = time.time()
            if holder == str(os.getpid()):
                return True                       # 自己上一轮漏放的锁，直接续用
            dead = bool(holder) and not _pid_alive(holder)
            if dead or (time.time() - ts) > ORDER_LOCK_TTL:
                try:
                    os.remove(path)
                except Exception:
                    pass
                continue
            return False
        except OSError:
            return False
    return False


def release_order(order_sn):
    """只放自己那把锁：按文件名直接删会把正在刷的窗口打断，造成两窗同时发批。"""
    path = _lock_path(order_sn)
    try:
        with open(path, encoding='utf-8') as f:
            holder = f.read().split()[0]
    except Exception:
        return
    if holder == str(os.getpid()):
        try:
            os.remove(path)
        except Exception:
            pass


def order_busy(order_sn):
    """只读探测这单是不是别的窗口正在刷。True 表示本窗口本轮不用管它。

    为什么要这一步：30 个窗口下，任何一页里剩下的单基本都在被别人刷。
    老流程是「先打详情接口 → 再看平台 execState → 再抢本机锁」，前两步都是
    HTTP 往返，两条全白打才轮到本机锁发现「窗口忙」。本机锁文件读一次只要
    几百微秒，所以先在本地判一次，能把这部分空转请求整段砍掉。
    注意这里只读不抢：抢锁仍然是 claim_order 的事，判活/过期口径必须一致，
    否则会出现「本地判忙跳过」和「锁已过期可抢」两套结论打架。
    """
    path = _lock_path(order_sn)
    try:
        with open(path, encoding='utf-8') as f:
            parts = f.read().split()
    except Exception:
        return False                      # 没锁文件 = 没人刷，正常往下走
    holder = parts[0] if parts else ''
    try:
        ts = float(parts[1]) if len(parts) > 1 else os.path.getmtime(path)
    except Exception:
        ts = time.time()
    if holder == str(os.getpid()):
        return False                      # 自己的锁：上一轮漏放，照常进去续用
    if not holder:
        return False
    if not _pid_alive(holder):
        return False                      # 持锁窗口已经没了，claim_order 会清掉它
    return (time.time() - ts) <= ORDER_LOCK_TTL


def ceiling_hit(aweme_id: str, result: dict, day_cap: int = None) -> bool:
    """今天进过量、这轮零增量、本地额度还没满、且今天已经吃到接近抖音日顶——四条同时成立才算平台天花板。

    为什么用「今天进过量」当判据：真死的视频今天一条都进不去；
    进过几百条说明通道是通的、设备是活的，卡住的是这条视频的当日记账预算。
    为什么还要看本地 room：本地真到 1800 属于「当日额度用尽」，那是另一条分支
    的职责，这里不要重复处理，否则两条分支会互相抢单。
    为什么要加 CEIL_MIN_TODAY：以前只要当天进过 1 条、这轮又零增量，就判天花板并把单子
    推到次日 0 点，等于把整天的关窗当成了日额度用尽。3246292 停在 1098、3246294 停在
    1114 就是这么来的——买家看六小时不动直接申请退单。真正的日顶在 1200 上下，
    没到 1000 之前零增量一律按限流处理，退避后继续试。
    """
    if int(result.get('delta') or 0) > 0:
        return False
    used = U.video_day_used(aweme_id)
    if used <= 0:
        return False
    cap = int(VIDEO_DAY_CAP if day_cap is None else day_cap)
    if used < ceil_min_today(cap):
        return False
    if U.video_room(aweme_id, cap) <= LOSE_NUM:
        return False
    return True


SHARE_GAP = int(os.environ.get('SHARE_DEVICE_GAP', 300))  # 同设备两次分享最小间隔（秒）
# 每轮只吃这么多，吃完回到队尾等下一轮：12 个进程 × 100 单时，
# 一次把 1800 全冲完会让前 12 单占满设备、后 88 单当天颗粒无收。
# 180 这个量级还有第二个作用：实测服务端对单条视频的计数窗口一次只认 220 上下，
# 一轮塞进去 400 条，后 180 条会被「收下但不计数」，纯属白烧设备。
ROUND_CHUNK = int(os.environ.get('SHARE_ROUND_CHUNK', 180))

# ---- 节奏层（pace_ledger）：冲刺段 + 尾段
# 现在的口径是「能刷多少刷多少」，冲刺段占满整单，尾段那套曲线不再参与限速。
# PACE_HOURS   整单摊多少小时。原来 12 小时，跨零点后每段的 deadline 会被截到
#              几小时，剩量大的单第二天只能挤在几小时里；改 96 小时（4 天交付期），
#              跨天重开段时每天都是完整一天，不再被截短。
# PACE_HEAD_PCT 冲刺段吃下单量的多少。1.0 = 剩余量全部算冲刺额度，head_target
#              直接等于整单目标，曲线闸 / 小时闸 / 日配额闸三道全不生效。
# PACE_HEAD_MIN 冲刺段持续多少分钟。10080 = 7 天，覆盖整个交付周期。
#              不会无限冲刺：零增量立刻 end_head 转尾段，再按 600/1200/… 阶梯重开，
#              最多 head_retries=6 次，关窗期的空转照样被挡住。
# PACE_HEAD_CHUNK 冲刺段单批上限，180 与 daily1800 的实测窗口容量一致
# PACE_SHAPE   tail 段形状 linear 匀速 / front 前重后轻 / back 后重前轻
# PACE_CHUNK   尾段单批上限，慢速档 40 条一批落账确认成本更低
# PACE_MIN_BATCH 尾段低于这个条数本轮跳过，攒够一批再来
# PACE_PER_DAY 每自然日最多入账多少条；0 = 不限，只受 DAY_CAP 与 duration 约束
# PACE_SKIP    尾段没到点时，这单最少等多久再看
PACE_HOURS = float(os.environ.get('SHARE_PACE_HOURS', 96))
PACE_HEAD_PCT = float(os.environ.get('SHARE_PACE_HEAD_PCT', 1.0))
PACE_HEAD_MIN = float(os.environ.get('SHARE_PACE_HEAD_MIN', 10080))
PACE_HEAD_CHUNK = int(os.environ.get('SHARE_PACE_HEAD_CHUNK', 180))
PACE_SHAPE = os.environ.get('SHARE_PACE_SHAPE', 'linear')
PACE_CHUNK = int(os.environ.get('SHARE_PACE_CHUNK', 40))
PACE_MIN_BATCH = int(os.environ.get('SHARE_PACE_MIN_BATCH', 6))
PACE_PER_DAY = int(os.environ.get('SHARE_PACE_PER_DAY', 0))
PACE_JITTER = float(os.environ.get('SHARE_PACE_JITTER', 0.35))
PACE_SKIP = float(os.environ.get('SHARE_PACE_SKIP', 120))
# PACE_HEAD_RETRY 冲刺被零增量打断后多久重开：实测窗口 5~140 分钟重开一次，
# 一次打死就永久转尾段等于把每次重开的窗口都让给尾段的十几条/小时。
PACE_HEAD_RETRY = float(os.environ.get('SHARE_PACE_HEAD_RETRY', 600))

DRY = len(sys.argv) > 4 and str(sys.argv[4]).lower() == 'dry'

# 同一批窗口吃的货号：1594 和 997 都是「分享量」类，配置里默认只挂一个，
# 结果一半窗口在别的货号上永远空转。这里用逗号把要吃的货号全列出来，
# 每个窗口每圈把每个货号的单子都拉一遍再合并，谁的货号都能被抢到。
_raw_goods = os.environ.get('SHARE_GOODS') or ''
GOODS_LIST = [g.strip() for g in _raw_goods.split(',') if g.strip()]
if not GOODS_LIST:
    GOODS_LIST = [str(getattr(function, 'goods_sn', '') or '').strip()]
GOODS_LIST = [g for g in GOODS_LIST if g]
if not GOODS_LIST:
    GOODS_LIST = ['1594']


def goods_day_cap(goods: str) -> int:
    """货号 → 单视频每日入账上限。0 = 不限。

    为什么从配置文件读而不是写死在代码里：日限是跟着货源走的商务参数
    （1594 限 1200、997 不限），换一次货源就要改一次代码等于每次都要重发版本；
    写进 cfg_qw_dy_share_<货号>.json 的 video_day_cap，改配置即生效。
    配置里没写这个键的退回 SHARE_DAY_CAP 全局口径，老配置行为不变。
    """
    path = os.path.join(BASE_DIR, 'cfg_qw_dy_share_{}.json'.format(goods))
    try:
        with open(path, encoding='utf-8') as fh:
            raw = json.load(fh).get('video_day_cap')
    except Exception as exc:
        show_message('配置', '日限读取失败，退回全局口径',
                     '{} | {}'.format(goods, str(exc)[:80]))
        return int(VIDEO_DAY_CAP)
    if raw is None:
        return int(VIDEO_DAY_CAP)
    try:
        return max(0, int(raw))
    except Exception:
        return int(VIDEO_DAY_CAP)


# 每个货号一份日限，进程启动时读一次：配置文件在运行期不会被改，
# 而每张单都去开文件读盘没必要（一台机器 30 个窗口 × 每秒十几单）。
DAY_CAP_BY_GOODS = {g: goods_day_cap(g) for g in GOODS_LIST}

# 设备池全进程共用：冷却状态跨订单保持，避免同一台设备在相邻订单里被连点
DEVICE_POOL = DevicePool(devices34 + load_extra_devices())
INFO_POOL = InfoPool(infos + load_extra_infos())
DEVICE_POOL.gap = SHARE_GAP
INFO_POOL.gap = SHARE_GAP

looper_num = 0
zero_rounds = {}            # order_sn -> 连续零增量轮数
day_skip = {}               # order_sn -> 该单什么时候可以再跑（当日额度用尽就推到明天零点）
zero_skip = {}              # order_sn -> 零增量退避到什么时候再试
pace_skip = {}              # order_sn -> 曲线没到点，这单什么时候再来看
LIST_LOG_EVERY = float(os.environ.get('SHARE_LIST_LOG', 30))  # 订单数量这行最多多久打一次（秒）
SKIP_LOG_EVERY = float(os.environ.get('SHARE_SKIP_LOG', 60))  # 窗口忙/未到点这类提示每单多久打一次
last_list_ts = 0.0          # 上次打订单数量的时间
last_list_n = -1            # 上次打的订单数量，数量变了立刻打，不用等 30s
skip_log_ts = {}            # order_sn -> 上次打这个单的跳过提示的时间
zero_since = {}             # order_sn -> 这一串零增量是从什么时候开始的
last_settle_ts = 0.0        # 上次到期结算扫描的时间

# 版本横幅：窗口一开就打印，用来确认跑的是这份代码而不是某个旧副本。
# 为什么必须打：3253143 / 3253177 被误判完成时，跑的正是没有基线补丁的旧副本，
# 而日志里没有任何一行能区分新旧，只能靠肉眼看路径。有了这行，路径和版本一起露出来。
print('[分享端] 版本 {} | {} | 缺量口径=本单基线台账，无基线不判完成'.format(
    CODE_VERSION, os.path.abspath(__file__)))
# 日限横幅：1200 / 不限 这类口径必须一开窗就能看见，否则日志里看不出跑的是哪套额度。
print('[分享端] 货号日限 {} | 全局口径 {} | 0 或负数=不限'.format(
    ' '.join('{}={}'.format(g, (DAY_CAP_BY_GOODS.get(g) or '不限'))
             for g in GOODS_LIST),
    VIDEO_DAY_CAP))


def order_link(order) -> str:
    """从订单 params 里取作品链接。params 是 [{alias,name,value}] 数组。"""
    for p in order.get('params') or []:
        value = str(p.get('value') or '')
        if 'douyin.com' in value or value.isdigit():
            return value
    return ''


def run_booster(aweme_id: str, target: int, day_cap: int = None) -> dict:
    """分享通道上报。field 用已确认的 share_delta，计数器用官方 share_count。

    day_cap 传本单货号的单视频每日上限：1594 = 1200，997 = 0（不限）。
    为什么按货号传：两条货号的日限不一样，用同一个全局值就得给两条线各起一套进程；
    而 997 不限，必须传 0 —— booster 里每一处额度判断都写在 `if self.day_cap:` 里，
    0 等于整段跳过（预留名额 / 收工复核 / 台账校准三处都不再夹额度），
    传大数字反而会在 10 万那一点上重新夹一次。
    """
    cap = VIDEO_DAY_CAP if day_cap is None else max(0, int(day_cap))
    booster = ShareBooster(aweme_id, target, threads=THREADS, timeout=BUDGET,
                           channels=CHANNELS, verbose=True, field=SHARE_FIELD,
                           counter=share_count, day_cap=cap,
                           device_pool=DEVICE_POOL, info_pool=INFO_POOL)
    return booster.run()


# ---- 单次调用版的两个回写：结算专用
# 为什么不用 function.update_order_refund / update_order_state：那两个是 while True，
# 接口一挂就把当前窗口钉死在这条单上，30 个窗口会陆续全卡住；更糟的是它们把异常
# 全吞在循环里，外面拿不到失败信号，台账会以为报成功了，之后不再重报 —— 漏退。
# 这里只发一次，失败直接抛，由 order_settle.settle 保持「未报」状态，下一轮再试。
def refund_once(order_sn: str, number: int, remark: str = 'remark'):
    """向平台报一次退款条数（只报条数，钱由平台按条数原路退）。"""
    q = '/api/supplier/order/v2/refund'
    data = {'orderSN': order_sn, 'number': int(number), 'remarks': remark}
    res = http_client.post(f'{function.api_url}{q}', headers=function.get_headers(q, True),
                           json=data, timeout=20)
    if not function.api_ok(res):
        raise RuntimeError(f'refund HTTP {res.status_code} {res.text[:120]}')
    return res.text


def state_once(order_sn: str, state: int = 1, remark: str = 'remark'):
    """向平台写一次订单状态（4 完成 / 7 退款完成）。"""
    q = '/api/supplier/order/v2/order'
    data = {'orderSN': order_sn, 'state': int(state), 'remarks': remark}
    res = http_client.post(f'{function.api_url}{q}', headers=function.get_headers(q, True),
                           json=data, timeout=20)
    if not function.api_ok(res):
        raise RuntimeError(f'state HTTP {res.status_code} {res.text[:120]}')
    return res.text


def current_share(order_sn: str, aweme_id: str, start_num: int) -> int:
    """只返回官方 share_count 原值；读不到返回 -1，绝不拿平台数折算。

    为什么删掉从前那条 `start_num + execNum` 兜底：execNum 和 currentNum 都是
    平台自己记的进度，而平台记的进度不准 —— 实测重新放行过的单 execNum 回 0
    （3246294 实际已交付 148 也回 0），平台上 12 条单里有 4 条比真实分享量少
    11~55 条。这个数一旦经 settle_try 的 final 参数流进 credited，退款条数
    = 下单量 - credited，平台的误差就直接变成多退或多收的钱。
    读不到就返回 -1：上层不记账、不判完成、不改基线，单子留在原地等下一轮。
    """
    return share_count(aweme_id)


def ensure_ledger(order_sn: str, aweme_id: str, total: int, goods: str = '',
                  day_cap: int = 0, base: int = 0):
    """给一条还没接管的单先建上账，幂等。

    为什么要在「今天额度被别人吃满」这种啥都没干的分支里也建账：
    不建账，结算扫描永远看不见这条单，它会一直挂在平台「处理中」，
    到期既不完成也不退款 —— 没人认领的单就是烂账。
    这里只建账不 note：note 会把 credited 覆盖成本轮的读数，
    一条今天已经刷了 900 的单如果在这时候 note(0)，账就抹平了。
    """
    if DRY:
        return None
    rec = S.plan(order_sn, aweme_id or '', int(total or 0), goods=goods,
                 day_cap=int(day_cap or 0), base=int(base or 0), dry=False)
    return rec


# ---- 结算：到期单报退款 / 写完成
# 为什么三处调用点共用这一个函数：什么时候能结、退多少，只能有一个判据
# （order_settle.decide）。1161 条误退那次的根子就是三处各写一套判据、口径不一致。
def settle_try(order_sn: str, looper_num: int = 0, final: int = None, why: str = '',
               now: float = None):
    """对一条单做一次结算决策并执行。返回 decide 结果或 settle 结果；无需结算返回 None。

    final：调用方手上已有的分享量读数。传 None 或负数就自己去读一次。
    now：只在自测里用，注入「当前时刻」，生产不传。
    退款条数只认「官方净增 = 当前 share_count - 本单基线」，不认回执条数：
    回执是本地发出去的次数，计入才是平台真正加上的量，两者差 30%~60%。
    """
    if DRY:
        return None
    rec = S.load(order_sn)
    if not rec or str(rec.get('status')) != 'in_progress':
        return None
    # 结算互斥：同一单同一时刻只有一个窗口在报退款，否则平台会被报两次
    if not S.lock(order_sn):
        return None
    try:
        rec = S.load(order_sn) or rec
        if str(rec.get('status')) != 'in_progress':
            return None
        # 先看标的是否还在：用户删了 / 自己设成私密 / 被平台封禁，一律按完成结算、一分不退。
        # 必须先于计数判断：视频删掉后 detail 仍可能返回删除前那份 statistics，
        # 拿着旧值算进度会一直显示「还差很多」，白等到末天才结，客户还以为是限流。
        gone = video_state(rec.get('aweme_id') or '')
        if gone in ('deleted', 'private', 'prohibited'):
            rec['video_gone'] = gone
            credited = int(rec.get('credited') or 0)
            res = S.settle(rec, credited, 'done', refund_once, state_once,
                           dry=False, now=now)
            show_message(f'第 {looper_num} 次 | {order_sn}', '视频已不存在',
                         f'{credited}/{rec.get("total")} | 判定={gone} | '
                         f'按完成处理不退 | {res.get("msg")}')
            return res
        value = final
        if value is None or value < 0:
            value = share_count(rec.get('aweme_id') or '')
        if value < 0:
            fails = S.note_fail(rec)
            show_message(f'第 {looper_num} 次 | {order_sn}', '结算待定',
                         f'分享量读不到（第 {fails}/{S.FAIL_DEAD} 次），本轮不结')
            if fails < S.FAIL_DEAD:
                return None
            credited = int(rec.get('credited') or 0)   # 连续读不到：按最后一次已知量结
        else:
            credited = max(0, int(value) - int(rec.get('base') or 0))
            S.note(rec, credited, now=now)
            rec = S.load(order_sn) or rec
        # video_gone 传本次探测结果，不传台账里的历史标记：L520 已经处理过
        # 「本次探到删除/私密/封禁」并直接返回了，能走到这里就是本次没探到。
        # 拿 rec['video_gone'] 传进去的后果：视频先被删、后来用户恢复，读数又恰好
        # 连续失败，就会按残留标记判完成、一分不退 —— 那是拿旧状态吃掉客户的钱。
        act = S.decide(rec, credited, now=now, video_gone=False)
        if act['action'] not in ('done', 'refund'):
            return act
        res = S.settle(rec, credited, act['action'],
                       refund_once, state_once,
                       dry=False, now=now)
        show_message(f'第 {looper_num} 次 | {order_sn}',
                     '订单完成' if act['action'] == 'done' else '订单退款',
                     f'{credited}/{rec.get("total")} | {act["reason"]} | {res.get("msg")}')
        return res
    except Exception as exc:
        show_message(f'第 {looper_num} 次 | {order_sn}', '结算异常', str(exc)[:120])
        return None
    finally:
        S.unlock(order_sn)


def settle_sweep(looper_num: int, now: float = None) -> int:
    """到期结算扫描，主循环每圈跑一次，返回本轮结掉几条。

    为什么必须独立于打量：单被打进 day_skip 推到次日零点之后，打量分支整整一天
    都不会再碰它；只在打量路径里结算，等于「今天没刷完」的单永远等不到退款。
    """
    if DRY:
        return 0
    done = 0
    for rec in S.due(now, batch=SETTLE_BATCH):
        sn = str(rec.get('order_sn') or '')
        if not sn or order_busy(sn):
            continue          # 另一个窗口还在刷这条单，等它收工再结
        res = settle_try(sn, looper_num, now=now)
        # ok=False 表示平台没确认（退款接口报错 / 状态写不进去）：
        # 这时候不能算结完，也不能放视频，下一轮还得接着试。
        if isinstance(res, dict) and res.get('ok') and res.get('action') in ('done', 'refund'):
            done += 1
            B.video_release(rec.get('aweme_id') or '', sn)
    return done


# ---- 自动接单：把平台挂在「已付款(1)」的分享单自己推进到「处理中(2)」
# 为什么要在本机做：播放标品是平台自家货源，客户付完款 1~5 秒平台就自动置 2；
# 1594 / 997 这种自发货源平台不给自动接单开关，单子会一直停在 1 等人点。
# 实测 3249467、3247753 挂在 1 上几个小时没人碰，客户那边看到的就是「没开工」。
# 接单只是把 1 写成 2，不动数量、不动金额、不回写任何执行数据，剩下的全交给缺量循环。
AUTO_ACCEPT = str(os.environ.get('SHARE_AUTO_ACCEPT', '1')).lower() not in ('0', 'false', 'no', 'off')
# 每圈最多接几张：一次把几百张全置 2，会让所有窗口同时去抢同一批单的锁，
# 抢不到的原地空转；30 张一批，够 30 个窗口分工，也不会把列表撑爆。
INTAKE_MAX = int(os.environ.get('SHARE_INTAKE_MAX', 30))


def promote_paid(looper_num) -> list:
    """拉「已付款」单子并置为处理中，返回本圈新接进来的订单对象。"""
    pulled = []
    for _goods in GOODS_LIST:
        if len(pulled) >= INTAKE_MAX:
            break
        try:
            paid = function.get_order_list('1', goods_sn=_goods) or []
        except Exception as exc:
            # 拉取失败不能拖垮整圈：这一圈照旧吃已经接进来的单，下一圈再试。
            show_message('自动接单', f'货号 {_goods} 拉取失败', str(exc)[:120])
            continue
        for order in paid:
            if len(pulled) >= INTAKE_MAX:
                break
            sn = str(order.get('orderSN') or '')
            if not sn:
                continue
            # 置 2 之后本圈就把这条单当处理中单排进队尾，不必等下一圈再拉一次列表。
            # 用单次版接单：接单接口挂了就跳过这一条，别把整圈卡在 while True 里。
            try:
                msg = state_once(sn, 2, '自动接单')
            except Exception as exc:
                show_message(f'第 {looper_num} 次 | {sn}', '接单失败', str(exc)[:100])
                continue
            order['state'] = 2
            # 打上货号标记：下面算单视频日限要靠它区分 1200 与不限，
            # 平台的订单对象不带 goodsSN，只能在这里补。
            order['_goods'] = _goods
            pulled.append(order)
            show_message(f'第 {looper_num} 次 | {sn}', '自动接单',
                         f'货号 {_goods} | 下单 {order.get("number")} 条 | '
                         f'已置处理中 | {str(msg)[:60]}')
    return pulled


# ======================================================================
# 调度层（页池 / 预取 / 让位）：把「等」从窗口里拿掉
#
# 老结构：一个窗口 = 一个固定页 + 一条一条串行跑，页里没单就睡。实测 30 个窗口
# 里只有 9 个页上有单（997 的 pages=[50,50,50,50,4,0,…]），另外 21 个窗口整晚
# 空转 —— 有效并行只有 9/30=30%。三个动作：
#   1 页池共享：页不再绑死在窗口上。窗口领页 → 干完让出来 → 下一轮重新领，
#     领不到就说明 30 页都被活窗口占着（这时才该睡），有效并行回到 30/30。
#   2 预取队列：跑当前这单的时候，后台线程已经把下一单的详情/平台占位/视频额度
#     预先取回来了，换手不再等那 5 次串行往返（实测 8~20 秒）。
#   3 让位：撞额度上限、零增量、曲线未到点时立刻放掉视频归属和本机订单锁，
#     同一视频的下一条单、以及同一个队列里的下一个窗口马上能接。
# ======================================================================
PAGE_MAX = int(os.environ.get('SHARE_PAGE_MAX', 30))       # 页池大小：一条货号有多少页可扫
PAGE_SWEEP = int(os.environ.get('SHARE_PAGE_SWEEP', 6))    # 一轮最多连领几页，全空才认「池里没活」
PAGE_CLAIM_TTL = float(os.environ.get('SHARE_PAGE_CLAIM_TTL', 900))   # 页归属多久没续租可回收（秒）
IDLE_SLEEP = float(os.environ.get('SHARE_IDLE_SLEEP', 15))  # 整池都没活时的一个整觉（秒，封顶）
PREFETCH_ON = str(os.environ.get('SHARE_PREFETCH', '1')).lower() not in ('0', 'false', 'no', 'off')
PREFETCH_DEPTH = int(os.environ.get('SHARE_PREFETCH_DEPTH', 2))   # 预取队列深度（条）
PREFETCH_TTL = float(os.environ.get('SHARE_PREFETCH_TTL', 300))   # 预取结果多久算过期（秒）
OUR_PID = os.getpid()
_fixed_page = str(sys.argv[2]) if len(sys.argv) > 2 and str(sys.argv[2]).isdigit() else '1'
PWIN = max(1, min(PAGE_MAX, int(_fixed_page)))   # 本窗口首选页，也是旋转起手位


def _page_path(page):
    return os.path.join(BASE_DIR, 'page_{}.claim'.format(int(page)))


def _page_read(page):
    """读页归属：(pid, ts)。读不到就是 0/0 —— 当无人占用处理。"""
    try:
        with open(_page_path(page), 'r', encoding='utf-8') as fh:
            rec = json.load(fh) or {}
        return int(rec.get('pid') or 0), float(rec.get('ts') or 0.0)
    except Exception:
        return 0, 0.0


def _page_write(page, exclusive=False):
    """写页归属。exclusive=True 用 O_CREAT|O_EXCL 抢首占，抢不到返回 False。

    为什么用文件而不是内存共享：30 个窗口是 30 个进程，跨进程只有磁盘能共享。
    为什么写 tmp 再 os.replace：replace 在同一分区上是原子的，读者要么看到旧内容
    要么看到新内容，不会读到半截 JSON。
    """
    tmp = '{}.{}.tmp'.format(_page_path(page), OUR_PID)
    body = json.dumps({'pid': OUR_PID, 'ts': round(time.time(), 1)}).encode('utf-8')
    try:
        if exclusive:
            fd = os.open(_page_path(page), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                os.write(fd, body)
            finally:
                os.close(fd)
            return True
        with open(tmp, 'wb') as fh:
            fh.write(body)
        os.replace(tmp, _page_path(page))
        return True
    except FileExistsError:
        return False
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False


def claim_page(prefer=None):
    """从页池领一页。成功返回页号；30 页都被活窗口占着时返回 None。

    为什么从自己那一页开始找：launcher 给每个窗口的起始页本来就不同（p1..p30），
    以自己那页起手，第一轮 30 个窗口正好把 30 页占满、互不重叠。
    为什么失败要返回 None 而不是硬用自己的页：返回 None 说明此刻池里没有无主页，
    这一圈真该睡 —— 硬用自己的页会让 21 个空页窗口回到空转老路。
    """
    start = int(prefer or PWIN)
    now = time.time()
    for _i in range(PAGE_MAX):
        page = (start - 1 + _i) % PAGE_MAX + 1
        owner, ts = _page_read(page)
        if owner == OUR_PID:
            _page_write(page)                  # 续租，别让 TTL 把自己这页收走
            return page
        if owner and _pid_alive(owner) and (now - ts) <= PAGE_CLAIM_TTL:
            continue                           # 别人正拿着，看下一页
        if owner:
            if _page_write(page):              # 占位进程已经不在了：直接接管
                return page
            continue
        if _page_write(page, exclusive=True):  # 无主页：抢首占
            return page
    return None


def release_page(page):
    """让页：只删自己那一份，别人的占位文件不动。"""
    if page is None:
        return False
    owner, _ts = _page_read(page)
    if owner != OUR_PID:
        return False
    try:
        os.remove(_page_path(page))
        return True
    except Exception:
        return False


def prepare_order(order):
    """一条单的网络前置一次做完：详情 → 平台占位 → 换 ID → 查视频额度。

    返回 dict，主循环只认字段，不再自己发这几个请求：
        ok 能不能接着刷 | kind ok/state/taken/bad_id/capped/error
        detail 详情对象 | aweme_id 19 位视频 ID | room 该视频今日剩余条数
        day_cap 本单货号日限 | goods_key 本单货号
    为什么单独抽出来：这四步彼此独立、又不依赖 booster，抽出来才能搬到后台线程
    预取。原地不动的部分（基线、曲线、上报、结算）必须留在主线程串行。
    """
    sn = str(order.get('orderSN') or '')
    goods_key = str(order.get('_goods') or order.get('goodsSN')
                    or (GOODS_LIST[0] if GOODS_LIST else ''))
    day_cap = DAY_CAP_BY_GOODS.get(goods_key, int(VIDEO_DAY_CAP))
    rec = {'ok': False, 'kind': 'error', 'reason': '', 'detail': {}, 'aweme_id': '',
           'room': 0, 'day_cap': day_cap, 'goods_key': goods_key, 'ts': time.time()}
    try:
        detail = function.get_order_detail(sn)
    except Exception as exc:
        rec['reason'] = '详情接口异常 {}'.format(str(exc)[:80])
        return rec
    if not detail or int((detail or {}).get('state') or 0) != 2:
        rec['kind'] = 'state'
        rec['reason'] = '详情状态非处理中'
        rec['detail'] = detail or {}
        return rec
    rec['detail'] = detail
    try:
        state = int(function.upload_server_content('execState?order_sn={}'.format(sn)))
    except Exception:
        state = 2                              # 查不到就按「没被领走」继续，交给本机锁兜底
    if state == 1:
        rec['kind'] = 'taken'
        rec['reason'] = '平台侧已被领取'
        return rec
    try:
        aweme_id = str(function.upload_server_content(
            'execute?order_sn={}&exec_update=1'.format(sn)) or '').strip()
    except Exception as exc:
        aweme_id = ''
        rec['reason'] = 'execute 异常 {}'.format(str(exc)[:60])
    if len(aweme_id) != 19:
        aweme_id = resolve_aweme_id(order_link(detail) or order_link(order) or '')
    if len(aweme_id) != 19:
        rec['kind'] = 'bad_id'
        rec['reason'] = rec['reason'] or '取不到视频ID'
        return rec
    rec['aweme_id'] = aweme_id
    room = U.video_room(aweme_id, day_cap)
    rec['room'] = room
    if room <= LOSE_NUM:
        rec['kind'] = 'capped'
        rec['reason'] = '视频 {} 今日额度已用尽'.format(aweme_id)
        return rec
    rec['ok'] = True
    rec['kind'] = 'ok'
    return rec


class Prefetcher(object):
    """后台预取线程：主线程跑 booster 的时候，它把下一条候选单的前置请求先打出去。

    为什么值得单开线程：换手要 8~20 秒，其中 5 次串行往返占了绝大部分，而这 5 次
    与 booster 完全无依赖。放后台打，主线程到点直接取结果，换手降到 1 秒内。
    线程安全的前提（已核对）：function 里只有一个 httpx.Client（自身线程安全，
    keepalive 300）；used_store 用 threading.local() 每线程一条连接；订单锁和
    视频锁只在主线程里抢，后台线程只读不写上报。
    """

    def __init__(self, depth=2, ttl=300.0):
        self.depth = max(1, int(depth))
        self.ttl = float(ttl)
        self._ready = {}
        self._queue = collections.deque()
        self._cond = threading.Condition()
        self._stop = False
        self.stat = {'feed': 0, 'prep': 0, 'hit': 0, 'stale': 0, 'fail': 0, 'busy': 0}
        self._th = threading.Thread(target=self._run, name='share-prefetch', daemon=True)
        self._th.start()

    def feed(self, orders):
        """主线程把后面的候选单塞进来；队列满了就丢，不阻塞主线程。"""
        if not orders:
            return
        now = time.time()
        with self._cond:
            for sn in [k for k, v in self._ready.items() if now - v.get('ts', 0) > self.ttl]:
                self._ready.pop(sn, None)
                self.stat['stale'] += 1
            seen = set(self._ready) | set(item[0] for item in self._queue)
            for order in orders:
                sn = str(order.get('orderSN') or '')
                if not sn or sn in seen:
                    continue
                if len(self._ready) + len(self._queue) >= self.depth:
                    break
                self._queue.append((sn, order))
                seen.add(sn)
                self.stat['feed'] += 1
            self._cond.notify()

    def take(self, order_sn):
        """取某条单的预取结果；没有或已过期返回 None（主线程就地同步做一遍）。"""
        with self._cond:
            prep = self._ready.pop(str(order_sn), None)
        if not prep:
            return None
        if time.time() - prep.get('ts', 0) > self.ttl:
            self.stat['stale'] += 1
            return None
        self.stat['hit'] += 1
        return prep

    def _run(self):
        while True:
            with self._cond:
                while not self._queue and not self._stop:
                    self._cond.wait(1.0)
                if self._stop:
                    return
                sn, order = self._queue.popleft()
            try:
                if not DRY and order_busy(sn):
                    self.stat['busy'] += 1
                    continue
                prep = prepare_order(order)
            except Exception:
                self.stat['fail'] += 1
                continue
            with self._cond:
                # 只缓存有结论的结果：瞬时异常（error）不缓存，否则一条网络抖动
                # 会让这条单在 TTL 内被跳过 300 秒。
                if prep.get('ok') or prep.get('kind') in ('state', 'taken', 'capped', 'bad_id'):
                    self._ready[sn] = prep
                    self.stat['prep'] += 1
                else:
                    self.stat['fail'] += 1


def _lookahead(orders, idx, depth):
    """从当前位置往后挑 depth 条「现在没有退避」的单交给预取线程。

    为什么先过一遍本地跳过表：预取是有代价的（一次 execute 就动了平台状态），
    把已经知道在退避里的单塞进去纯属白打接口。
    """
    if depth <= 0:
        return []
    now = time.time()
    picked = []
    for order in orders[idx + 1:]:
        if len(picked) >= depth:
            break
        sn = str(order.get('orderSN') or '')
        if not sn:
            continue
        if (day_skip.get(sn, 0) > now or zero_skip.get(sn, 0) > now
                or pace_skip.get(sn, 0) > now):
            continue
        if not DRY and order_busy(sn):
            continue
        picked.append(order)
    return picked


def pull_orders(tries=None):
    """按页池领页拉单。领到有单的页就留下，连领几页都空返回 (None, [])。

    为什么空页要立刻让出来：一个窗口抱着一页空页，等于把 1/30 的并行度钉死；
    让出来之后下一轮它可能领到一页有 50 条单的页，池子里的活就能自己流向闲窗口。
    """
    tries = PAGE_SWEEP if tries is None else max(1, int(tries))
    for _ in range(tries):
        page = claim_page()
        if page is None:
            return None, []
        function.page = str(page)
        orders = []
        for _goods in GOODS_LIST:
            part = function.get_order_list('2', goods_sn=_goods) or []
            for _o in part:
                _o['_goods'] = _goods
            orders.extend(part)
        if orders:
            return page, orders
        release_page(page)
    return None, []


# 预取线程只在正常接单时起：dry 是演练，不能有后台线程替它动平台状态。
PF = Prefetcher(PREFETCH_DEPTH, PREFETCH_TTL) if (PREFETCH_ON and not DRY) else None
print('[分享端] 调度 | 页池 {} | 首选页 {} | 连领 {} 页 | 预取 {} 深 {} | 整觉 {}s | '
      '零增量退避 {}s 起 | 页归属 TTL {}s'.format(
          PAGE_MAX, PWIN, PAGE_SWEEP,
          'on' if PF else 'off', PREFETCH_DEPTH if PF else 0,
          int(IDLE_SLEEP), int(ZERO_BACKOFF), int(PAGE_CLAIM_TTL)))


while True:
    try:
        looper_num += 1
        # 到期结算先跑：它跟页池、跟这一圈有没有单都无关。放在拉单前面，
        # 才能保证「已经被推到明天零点」的单每天都会被结掉，而不是永远挂着。
        if time.time() - last_settle_ts >= SETTLE_EVERY:
            last_settle_ts = time.time()
            if settle_sweep(looper_num):
                _st = S.stats()
                show_message(f'第 {looper_num} 次', '结算扫描',
                             f'进行中 {_st["in_progress"]} | 待确认 {_st["pending_confirm"]} | '
                             f'已完成 {_st["paid"]} | 已退款单 {_st["refunded"]} | '
                             f'累计退款 {_st["refund_total"]} 条')
        # 页池领页：不再让「一个窗口 = 一个固定页」把并行度钉在有单的页数上。
        # 领到的页这一轮归本窗口；页里没有活可干就把页让出去，下一轮重新领。
        page, order_list = pull_orders()
        # 接单是全局动作（把 state=1 的单置 2），跟本窗口拿到哪页无关：
        # 池子被占满时也要照做，否则已付款单会一直挂在 1 上没人开工。
        if AUTO_ACCEPT and not DRY:
            if page is None:
                function.page = str(PWIN)
            order_list.extend(promote_paid(looper_num))
        # 订单数量这行不再每圈打：数量没变就 30 秒一次，数量一变立刻打。
        # 每圈都打会在全单入退避时刷成每秒好几行，真看日志时反而找不到有用的行。
        if len(order_list) != last_list_n or (time.time() - last_list_ts) >= LIST_LOG_EVERY:
            show_message(f'第 {looper_num} 次',
                         f'订单数量({"+".join(GOODS_LIST)}) | 页:{page if page else "无"}',
                         str(len(order_list)))
            last_list_n = len(order_list)
            last_list_ts = time.time()
        if len(order_list) == 0:
            # 连领 PAGE_SWEEP 页都空，或者 30 页都被活窗口占着：这一圈是真没活。
            # 睡一个整觉（封顶 IDLE_SLEEP）再看，别一秒转两圈去灌平台订单接口。
            del order_list
            gc.collect()
            if DRY:
                show_message(f'第 {looper_num} 次', '演练结束', '整池无单，退出演练')
                raise SystemExit(0)
            time.sleep(IDLE_SLEEP)
            continue
        order_list.reverse()

        did_work = False           # 本页有没有真正发出过上报，用来决定这一圈要不要睡
        live_candidates = 0        # 过了本地跳过表的单数；为 0 说明这页现在全是退避单

        for _idx, order in enumerate(order_list):
            try:
                start_time = time.time()
                order_sn = str(order['orderSN'])
                order_num = int(order['number'])
                start_num = int(order['startNum'])
                _aweme = ''            # 让位与异常分支都要放视频锁，那时 aweme_id 未必已赋值
                # 本单货号决定它的单视频日限：1594 = 1200，997 = 0（不限）。
                # 必须在 get_order_detail 覆盖 order 之前取出来，详情接口不带 _goods 标记。
                goods_key = str(order.get('_goods') or order.get('goodsSN')
                                or (GOODS_LIST[0] if GOODS_LIST else ''))
                day_cap = DAY_CAP_BY_GOODS.get(goods_key, int(VIDEO_DAY_CAP))

                # 这条视频今天的额度已经用完：留单不动，明天零点后自动接着跑
                if day_skip.get(order_sn, 0) > time.time():
                    continue
                # 上一轮零增量：退避期内不碰，避免持续烧设备
                if zero_skip.get(order_sn, 0) > time.time():
                    continue
                # 曲线还没走到这条单的额度：跳过，别空转
                if pace_skip.get(order_sn, 0) > time.time():
                    continue
                # 本机锁前置判忙：省掉详情接口和平台 execState 两次往返。
                # 30 个窗口时空页窗口回落到 0-9 随机页，捞到的单九成都在别人手里，
                # 判忙放在最前面，一个空转窗口一圈的平台请求从 2N 降到 0。
                if not DRY and order_busy(order_sn):
                    if (time.time() - skip_log_ts.get(order_sn, 0)) >= SKIP_LOG_EVERY:
                        skip_log_ts[order_sn] = time.time()
                        show_message(f'第 {looper_num} 次 | {order_sn}', '窗口忙',
                                     '另一个窗口持锁中，未查详情直接跳过')
                    del order
                    continue

                # ---- 演练：只解析链接试打，不抢锁、不回写、不改状态
                if DRY:
                    aweme_id = resolve_aweme_id(order_link(order))
                    if len(aweme_id) != 19:
                        show_message(f'第 {looper_num} 次 | {order_sn}', '演练异常', '取不到视频ID')
                        del order
                        continue
                    room = U.video_room(aweme_id, day_cap)
                    if room <= 0:
                        show_message(f'第 {looper_num} 次 | {order_sn}', '演练跳过',
                                     f'视频 {aweme_id} 今日额度已用满（日限 {cap_text(day_cap)}）')
                        del order
                        continue
                    before = share_count(aweme_id)
                    result = run_booster(aweme_id, min(3, room), day_cap)
                    show_message(f'第 {looper_num} 次 | {order_sn}', '演练结果',
                                 f'{aweme_id} | 前:{before} | 后:{result["final"]} | '
                                 f'增量:{result["delta"]} | 接受:{result["accepted"]} | '
                                 f'通道:{result["channels"]} | 原因:{result["reason"] or "-"}')
                    del order
                    continue

                # 这条单本地确实有活可干；交给预取线程往后看几条，同时尝试取本条已预取的
                # 结果。为什么先 feed 再 take：feed 是给「下一条」的，take 是取「这一条」的。
                # 先喂后取，下一条的 5 次往返正好落在本条 booster 跑的那几十秒里。
                live_candidates += 1
                prep = None
                if PF:
                    PF.feed(_lookahead(order_list, _idx, PREFETCH_DEPTH))
                    prep = PF.take(order_sn)
                if not prep:
                    prep = prepare_order(order)     # 没预取到就地同步做一遍，行为与老版一致
                _aweme = prep.get('aweme_id') or ''
                _kind = prep.get('kind')
                if _kind == 'state':
                    if (time.time() - skip_log_ts.get(order_sn, 0)) >= SKIP_LOG_EVERY:
                        skip_log_ts[order_sn] = time.time()
                        show_message(f'第 {looper_num} 次 | {order_sn}', '状态异常', '跳过')
                    del order
                    continue
                if _kind == 'taken':
                    if (time.time() - skip_log_ts.get(order_sn, 0)) >= SKIP_LOG_EVERY:
                        skip_log_ts[order_sn] = time.time()
                        show_message(f'第 {looper_num} 次 | {order_sn}', '已被领取', '跳过')
                    del order
                    continue
                if _kind == 'bad_id':
                    # 换不到视频 ID = 这单永远交付不了，按已交付量退余下全部。
                    # 老写法直接写 state=3（异常）就撒手：钱留在平台、单挂在异常态，
                    # 后台没人会再去捞它，等于白收钱。这里当场建账并结算。
                    bad = S.plan(order_sn, '', order_num, goods=goods_key,
                                 day_cap=day_cap, base=0, dry=DRY)
                    done_n = int((bad or {}).get('credited') or 0)
                    res = S.settle(bad, done_n, 'refund', refund_once,
                                   state_once, dry=DRY)
                    show_message(f'第 {looper_num} 次 | {order_sn}', '换不到视频ID',
                                 f'下单 {order_num} 条 | 已交付 {done_n} | '
                                 f'退 {max(0, order_num - done_n)} | {res.get("msg")}')
                    del order
                    continue
                # 单视频每日上限前置检查：额度已经用满就不必读计数、不必开线程，
                # 直接把单子排到明天零点，省一次网络往返也省一批设备。
                if _kind == 'capped':
                    # 今天这条视频的额度被别的单吃满了，本单一轮都没动。
                    # 仍然要建账：不建账结算扫描就看不见它，到期永远不退。
                    ensure_ledger(order_sn, _aweme, order_num, goods_key,
                                  prep.get('day_cap') or day_cap,
                                  base=int(order.get('startNum') or 0))
                    day_skip[order_sn] = U.next_day_start()
                    show_message(f'第 {looper_num} 次 | {order_sn}', '当日额度已用尽',
                                 f'视频 {_aweme} 今日已入账 {U.video_day_used(_aweme)}'
                                 f'/{cap_text(prep.get("day_cap"))}（货号 {prep.get("goods_key")}），'
                                 f'明日 00:00 后自动续刷')
                    del order
                    continue
                if not prep.get('ok'):
                    # 前置失败（换 ID / 解析）先建账再跳过：这类失败往往只是当下的，
                    # 但万一一直失败到过期，账上没有它，平台那条单就永远卡在处理中。
                    ensure_ledger(order_sn, _aweme, order_num, goods_key, day_cap,
                                  base=int(order.get('startNum') or 0))
                    if (time.time() - skip_log_ts.get(order_sn, 0)) >= SKIP_LOG_EVERY:
                        skip_log_ts[order_sn] = time.time()
                        show_message(f'第 {looper_num} 次 | {order_sn}', '前置失败',
                                     (prep.get('reason') or '-')[:80])
                    del order
                    continue
                aweme_id = _aweme
                day_cap = prep.get('day_cap') or day_cap
                goods_key = str(prep.get('goods_key') or goods_key)
                room = int(prep.get('room') or 0)

                current = current_share(order_sn, aweme_id, start_num)
                # startNum=0 不是「起点是 0」，是这个货号的平台不下发基线（1594/997 实测全 0）。
                # 拿 0 直接算 净增 = 绝对值 - 0，等于把视频历史总分享量算成本次交付：
                # 3252608（下 100 条、视频历史 500）第一轮就 100-500=-400 <= 5，40 秒写 4 完成，
                # 一条都没发。3253143 / 3253177 是同一处病在旧副本上又犯了一次：
                # 视频已被前几单刷到 698，新单平台 startNum 仍回 0，6 秒内 100-698<0 直接判完成，
                # 平台侧留在 0/0。所以基线只认本单自己的台账，落不下去就整轮不判完成，
                # 绝不允许带着 start_num=0 继续往下算缺量 —— 那是唯一能把两行数算成负数的入口。
                start_num = B.ensure(order_sn, aweme_id, start_num)
                if start_num <= 0 and current >= 0:
                    start_num = B.put(order_sn, aweme_id, current)
                    if start_num > 0:
                        show_message(f'第 {looper_num} 次 | {order_sn}', '基线落地',
                                     f'平台 startNum=0 → 本地基线 {start_num}，缺量按本地算')
                if start_num <= 0:
                    show_message(f'第 {looper_num} 次 | {order_sn}', '基线未定',
                                 f'当前分享量 {current}，本地台账没建起来，本轮不判完成')
                    del order
                    continue
                # ---- 交付切片台账：这单分几天交付、今天还能吃多少、末天退多少
                # base 用刚落地的本地基线，绝不能用平台 startNum（1594/997 恒回 0）。
                srec = S.plan(order_sn, aweme_id, order_num, goods=goods_key,
                              day_cap=day_cap, base=start_num, dry=DRY)
                if int(srec.get('base') or 0) != int(start_num):
                    # 基线变了（接管视频 / 人工补量 / 上一单收口）：台账基线跟着走，
                    # 否则「累计净增」会把上一单刷的量算进本单，退款就会算少。
                    if DRY:
                        srec['base'] = int(start_num)
                        srec['credited'] = 0
                        srec['day_log'] = []
                    else:
                        srec = S.set_base(srec, start_num)
                # credited 只认官方 share_count。current < 0 是「这轮没读到」，
                # 这时候 note(0) 会把台账里已有的 credited 抹平，末天退款就按 0 交付算，
                # 等于整单白退。读不到就一个字都不写，沿用台账里最后一次真实读数。
                if current >= 0:
                    credited_now = max(0, current - start_num)
                    if not DRY:
                        S.note(srec, credited_now)
                else:
                    credited_now = int(srec.get('credited') or 0)
                    show_message(f'第 {looper_num} 次 | {order_sn}', '读数失败',
                                 f'沿用台账已知交付 {credited_now} 条，本轮不记账')
                quota = S.quota_today(srec, credited_now)
                day_idx = S.day_index(srec)
                # 每轮把 startNum / currentNum 回写平台。为什么每轮都写：
                # 从前只在基线落地那一刻写一次，之后平台一直是 0/0，「进度没动却已完成」
                # 这种单在后台看不出来，是这次两个单被误判完成却没人发现的原因。
                if current >= start_num:
                    function.update_order_progress(order_sn, start_num, current)
                surplus_num = order_num - (current - start_num)
                if surplus_num <= LOSE_NUM and current >= start_num:
                    # 净增够了就是完成：走统一结算入口，它把台账置 paid 再写 4，
                    # 重复触发也不会重复写（台账状态本就是挡板）。
                    settle_try(order_sn, looper_num, final=current, why='净增达标')
                    B.video_release(aweme_id, order_sn)   # 让位，同视频的下一单立刻能接管
                    zero_rounds.pop(order_sn, None)
                    zero_skip.pop(order_sn, None)
                    del order
                    continue

                # ---- 节奏层：曲线说此刻该涨到多少，就只吃那么多
                # 为什么不直接用 ROUND_CHUNK：180 一批本身已是分批，但循环没有时间轴，
                # 窗口开着时一小时就能跑完 1800，对外仍是一条陡坡。曲线层给的是横轴。
                rec = P.ensure(order_sn, order_num, start_num=start_num,
                               hours=PACE_HOURS, shape=PACE_SHAPE, per_day=PACE_PER_DAY,
                               head_pct=PACE_HEAD_PCT, head_min=PACE_HEAD_MIN)
                # ensure 不会覆盖老记录的 duration / head_pct / head_min（防重启重置曲线），
                # 所以跑在旧参数上的单要在这里就地改口径：不改的话它们永远停在
                # 12 小时摊平 + 35% 冲刺，冲刺窗口早过期，一整天只有尾段几十条/小时。
                rec = P.retune(order_sn, hours=PACE_HOURS,
                               head_pct=PACE_HEAD_PCT, head_min=PACE_HEAD_MIN) or rec
                done_real = (current - start_num) if current >= 0 else 0
                if done_real > int(rec.get('delivered') or 0):
                    # 账本落后于服务端（进程重启、人工补量）：先对齐再算额度，否则重复刷。
                    # 用 align 而不是 note(差值)：多个窗口同时读到旧账本时，差值会被各加一遍。
                    rec = P.align(order_sn, done_real)
                target = P.allowance(rec, chunk=PACE_CHUNK, min_batch=PACE_MIN_BATCH,
                                     jitter=PACE_JITTER, head_chunk=PACE_HEAD_CHUNK)
                # 三重夹：整单缺口、视频当日剩余额度、今日交付切片。
                # 第三个夹子是「多日单第一天只吃 1200」的执行点：2300 的单在第一天
                # 无论曲线允许多少，都只能吃到切片为止，剩下的才轮得到末天去跑。
                target = min(target, surplus_num, room, quota)
                if target < PACE_MIN_BATCH:
                    # 睡到「攒够一批」的时刻，不是固定 120 秒：固定值会让慢速档
                    # 每 2 分钟醒一次、每次只够发 5 条，检查次数比发出去的条数还多。
                    wait = max(PACE_SKIP, min(1800.0,
                               P.next_check(rec, need=PACE_MIN_BATCH) - time.time()))
                    pace_skip[order_sn] = time.time() + wait
                    show_message(f'第 {looper_num} 次 | {order_sn}', '未到点',
                                 f'曲线应达 {P.due(rec)} | 账本已入账 {rec.get("delivered")} | '
                                 f'缺量 {surplus_num} → {wait:.0f}s 后再看')
                    del order
                    continue
                # 抢本机订单锁：抢不到说明另一个窗口正在刷这单，直接跳过。
                # 为什么必须在本机锁：30 个窗口都会拿到同一个 execState=2，
                # 各算一次缺量各发一批，缺量是各算各的，超出来的量没人负责。
                if not claim_order(order_sn):
                    # 同一条单在 30 个窗口里被跳过是常态，按单节流，不然日志全是这行
                    if (time.time() - skip_log_ts.get(order_sn, 0)) >= SKIP_LOG_EVERY:
                        skip_log_ts[order_sn] = time.time()
                        show_message(f'第 {looper_num} 次 | {order_sn}', '窗口忙',
                                     '另一个窗口正在刷这单，本轮跳过')
                    del order
                    continue
                # 拿到锁后重读一次计数：上一个窗口可能刚刷完一批，
                # 用读单时那份旧 current 算缺量，会多刷出 100~300 条（3246295 超交 300 就是这么来的）。
                live = share_count(aweme_id)
                if live >= 0:
                    # 视频独占：判满用的是视频的绝对分享量，两条单同时刷同一条视频时，
                    # 对方刷出来的增量会被本单算进自己的净增 —— 3253143 / 3253177 就是
                    # 同时对着 7687127311098894452 跑，视频 698→876（两单各 100 条），
                    # 两单都按「净增 178 >= 100」写了完成。独占之后同一时刻只有一单吃增量。
                    got, prev = B.video_lock(aweme_id, order_sn, VIDEO_OWNER_TTL)
                    if not got:
                        show_message(f'第 {looper_num} 次 | {order_sn}', '同视频排队',
                                     f'视频 {aweme_id} 正由订单 {prev} 刷，本单让位，下一轮再抢')
                        release_order(order_sn)
                        del order
                        continue
                    if prev and prev != order_sn:
                        # 换手：上一单在这个视频上刷出来的量不属于本单，基线重设到此刻，
                        # 账本整条重建（restart=True，delivered 归零、曲线从此刻起算），
                        # 缺量按整单重算。不重设的话本单会白吃上一单的量。
                        start_num = B.force(order_sn, aweme_id, live)
                        current = live
                        rec = P.ensure(order_sn, order_num, start_num=start_num,
                                       hours=PACE_HOURS, shape=PACE_SHAPE,
                                       per_day=PACE_PER_DAY, head_pct=PACE_HEAD_PCT,
                                       head_min=PACE_HEAD_MIN, restart=True)
                        surplus_num = order_num
                        function.update_order_progress(order_sn, start_num, current)
                        # 账本也得跟着换基线：本单从此刻起算，上一单的量不是本单的交付。
                        # 不清的话接管那一刻 credited 里还挂着别人的量，末天退款会退少。
                        if DRY:
                            srec['base'] = int(start_num)
                            srec['credited'] = 0
                            srec['day_log'] = []
                        else:
                            srec = S.set_base(srec, start_num)
                            S.note(srec, 0)
                        show_message(f'第 {looper_num} 次 | {order_sn}', '接管视频',
                                     f'上一单 {prev} 已收口 | 基线重设 {start_num} | '
                                     f'缺量回到整单 {order_num}')
                        target = min(order_num, room)
                    elif live > current:
                        current = live
                        rec = P.align(order_sn, current - start_num)
                    surplus_num = order_num - (current - start_num)
                    # 硬夹：本轮目标永远不超过「下单量 - 已净增」，也不超过视频当日剩余额度，
                    # 也不超过今日切片（多日单靠这一条把第一天压在 1200）。
                    target = min(target, max(0, surplus_num), room,
                                 S.quota_today(srec, max(0, current - start_num)))
                if surplus_num <= LOSE_NUM and current >= start_num and start_num > 0:
                    settle_try(order_sn, looper_num, final=current, why='净增达标')
                    B.video_release(aweme_id, order_sn)
                    zero_rounds.pop(order_sn, None)
                    zero_skip.pop(order_sn, None)
                    day_skip.pop(order_sn, None)
                    pace_skip.pop(order_sn, None)
                    release_order(order_sn)
                    del order
                    continue
                if target <= 0:
                    # 今日切片吃满（多日单的第 1 天到第 days-1 天）与「暂时打不动」是两回事：
                    # 前者必须推到零点后按下一片继续，否则这单会每圈空转查详情，
                    # 把视频归属也占着，同视频的单全跟着排队。
                    if quota <= LOSE_NUM and day_idx < int(srec.get('days') or 1):
                        day_skip[order_sn] = U.next_day_start()
                        show_message(f'第 {looper_num} 次 | {order_sn}', '今日切片已满',
                                     f'第 {day_idx}/{srec.get("days")} 天 | 已交付 '
                                     f'{credited_now}/{order_num} | 余量 {surplus_num} 明日继续')
                    else:
                        show_message(f'第 {looper_num} 次 | {order_sn}', '本轮不用刷',
                                     f'当前:{current} | 缺量:{surplus_num} | 额度:{room} | '
                                     f'今日切片余 {quota}')
                    # 让位：一枪不放就不能占着视频归属，否则同视频的下一条单
                    # 整轮都在「同视频排队」里空转，白占一个窗口。
                    B.video_release(aweme_id, order_sn)
                    release_order(order_sn)
                    del order
                    continue

                phase = '冲刺' if P.in_head(rec) else '尾段'
                show_message(f'第 {looper_num} 次 | {order_sn}', f'开始上报[{phase}]',
                             f'视频ID:{aweme_id} | 当前:{current} | 缺量:{surplus_num} | '
                             f'今日额度:{room} | 应达:{P.due(rec)} | 本轮目标:{target}')

                result = run_booster(aweme_id, target, day_cap)
                did_work = True
                show_message(f'第 {looper_num} 次 | {order_sn}', '上报完成',
                             f'接受:{result["accepted"]} | 尝试:{result["attempts"]} | '
                             f'真实增量:{result["delta"]} | 通道:{result["channels"]} | '
                             f'中止原因:{result["reason"] or "-"}')

                # 服务端进度报「累计净增」，不报本批增量。
                # exec_num 若是覆盖语义，报本批会让后台进度永远停在最后一批的数字上，
                # 跟退款口径（下单量 - 累计净增）对不上，客诉时两边数据打架。
                final = share_count(aweme_id)
                if final < 0:
                    # 读不到就先记一次失败，连续 FAIL_DEAD(6) 次由结算入口按已交付量退缺口。
                    # 老写法第一次读不到就写 state=3：单一进异常态，退款接口也认不得，
                    # 钱锁在平台上，而这时候往往一条都不差。
                    act = settle_try(order_sn, looper_num, final=-1, why='读不到分享量')
                    if not act or act.get('action') not in ('done', 'refund'):
                        fails = int((S.load(order_sn) or {}).get('cred_fail') or 0)
                        show_message(f'第 {looper_num} 次 | {order_sn}', '读数失败',
                                     f'统计接口本轮没返回，累计失败 {fails}/{S.FAIL_DEAD} 次'
                                     f'（到 {S.FAIL_DEAD} 次按已交付量结算）')
                    release_order(order_sn)
                    del order
                    continue

                exec_num = final - start_num
                left = order_num - exec_num
                function.upload_server_content(
                    f'update?order_sn={order_sn}&exec_state=1&exec_num={exec_num}')
                # 批末必须把官方读数回写台账。为什么非写不可：退款条数 = 下单量 - 台账
                # credited，而 credited 只有「下一轮打量入口」和「结算扫描」两处会刷新。
                # 这一批刷完到下一轮之间只要读数连续失败，结算就拿着上一批的旧值算缺量，
                # 多退的正是这一批已经交付掉的量（实测 3325560 差 40 条、3313058 差 14 条）。
                if not DRY:
                    cur_srec = S.load(order_sn)
                    if cur_srec:
                        S.note(cur_srec, final - start_num)
                # 账本只记真实净增：result['delta'] 是 share_count 实测差，
                # 回执 accepted 实测虚高 3.5 倍（787 对 221），拿它记账会让单子提前判完成。
                # 这里直接用服务端实测 final - start_num 对齐（幂等），dropped 只做统计。
                P.align(order_sn, final - start_num)
                P.note(order_sn, 0, dropped=P.overdue(rec, chunk=PACE_CHUNK))
                show_message(f'第 {looper_num} 次 | {order_sn}',
                             f'更新进度 | 开始:{start_num} | 当前:{final} | 执行:{exec_num} | 缺量:{left}',
                             function.update_order_progress(order_sn, start_num, final))
                # 这一批已经结算完，锁立刻放开给别的窗口，别占着 40 分钟
                release_order(order_sn)

                if left <= LOSE_NUM or P.done(order_sn):
                    settle_try(order_sn, looper_num, final=final, why='净增达标')
                    B.video_release(aweme_id, order_sn)
                    zero_rounds.pop(order_sn, None)
                    zero_skip.pop(order_sn, None)
                    day_skip.pop(order_sn, None)
                    pace_skip.pop(order_sn, None)
                elif result.get('day_capped') or U.video_room(aweme_id, day_cap) <= LOSE_NUM:
                    # 当日额度用尽不是故障：订单留着，明天同一视频还能按同一口径再刷。
                    # 这里除了看 run() 的 day_capped 标记，还回查一次台账，避免并发
                    # 进程把这条视频的额度吃掉之后本进程还傻等着。
                    day_skip[order_sn] = U.next_day_start()
                    zero_rounds[order_sn] = 0
                    zero_skip.pop(order_sn, None)
                    # 让位：这条视频今天再也吃不下量，视频归属必须立刻放开，
                    # 否则同视频的另一条单会在 30 个窗口里排队排到天亮。
                    B.video_release(aweme_id, order_sn)
                    show_message(f'第 {looper_num} 次 | {order_sn}', '当日额度用尽',
                                 f'视频 {aweme_id} 今日已入账 {U.video_day_used(aweme_id)} 条'
                                 f'（货号 {goods_key} 上限 {cap_text(day_cap)}），缺量 {left} 留到明日继续')
                    # 到点就结、没到点就留：这一句是「额度用尽」这条分支唯一需要
                    # 跟结算打交道的地方，末天 22:30 之后这里会直接把缺口退掉。
                    settle_try(order_sn, looper_num, final=final, why='额度用尽')
                elif result['delta'] <= 0 and ceiling_hit(aweme_id, result, day_cap):
                    # 平台天花板：今天进过量、这轮零增量、本地额度还没满。
                    # 留单到零点，不计天数、不退单 —— 与播放端一致，缺量明天接着吃。
                    # 为什么还要 end_head：冲刺段没有退避，桶已经关了还按 180 一批
                    # 猛打就是纯烧设备；转尾段后小时闸会把节奏压到回补速率。
                    if P.in_head(rec):
                        P.end_head(order_sn, 'ceiling')
                    day_skip[order_sn] = U.next_day_start()
                    zero_rounds[order_sn] = 0
                    zero_skip.pop(order_sn, None)
                    B.video_release(aweme_id, order_sn)   # 天花板：视频归属立刻让出去
                    show_message(f'第 {looper_num} 次 | {order_sn}', '当日天花板',
                                 f'今日入账 {U.video_day_used(aweme_id)} 条即停 | '
                                 f'缺量 {left} 留到下一个切片 | 等窗口重开')
                    settle_try(order_sn, looper_num, final=final, why='平台天花板')
                elif result['delta'] <= 0:
                    # 零增量：视频吃不下量或计数通道被限，留单重试，永不退单。
                    # 退避按 600s 起翻倍、封顶 3600s，给视频留出恢复窗口；
                    # 不退避就会在几十秒内把同一批设备连着烧光，还一轮轮刷失败计数。
                    # 冲刺段遇到零增量必须立刻关掉冲刺：冲刺没有闸和退避，
                    # 桶已经关了再冲就是纯烧设备，转进尾段后小时闸会接管节奏。
                    if P.in_head(rec):
                        P.end_head(order_sn, 'zero_delta', retry=PACE_HEAD_RETRY)
                        show_message(f'第 {looper_num} 次 | {order_sn}', '冲刺结束',
                                     f'零增量，桶已关；转尾段按回补速率铺剩余 '
                                     f'{max(0, order_num - exec_num)} 条')
                    rounds = zero_rounds.get(order_sn, 0) + 1
                    zero_rounds[order_sn] = rounds
                    # 150/300/600/1200/2400/3600 封顶：纯退避。轮数只用于日志和 3600 秒上限，
                    # 攒到多少轮都不退单，和播放端「一直重试」是同一套语义。
                    # 基数从 600 降到 150：退避单本来就不占窗口（跳过即走），
                    # 卡 600 秒只是让这条单在本页白排一小时，早一档回来早一轮吃到量。
                    backoff = min(ZERO_BACKOFF * (2 ** (rounds - 1)), 3600)
                    zero_skip[order_sn] = time.time() + backoff
                    # 让位：零增量说明这条视频此刻吃不下量，视频归属立刻放开，
                    # 让同视频的另一条单（或另一个窗口）用它自己的节奏去试。
                    B.video_release(aweme_id, order_sn)
                    # 开头时间只记第一次，中间零星吃到几条会清空重算，避免把间断的零增量
                    # 攒成一段「连续挂死」
                    zero_since.setdefault(order_sn, time.time())
                    stuck_h = (time.time() - zero_since[order_sn]) / 3600.0
                    show_message(f'第 {looper_num} 次 | {order_sn}', '零增量',
                                 f'连续 {rounds} 轮 | 已挂 {stuck_h:.1f}h | '
                                 f'{backoff}s 后重试 | 留单待窗口重开 | '
                                 f'原因:{result["reason"] or "-"}')
                    settle_try(order_sn, looper_num, final=final, why='零增量')
                else:
                    zero_rounds[order_sn] = 0
                    zero_skip.pop(order_sn, None)
                    zero_since.pop(order_sn, None)
                    if result['throttled']:
                        if P.in_head(rec):
                            # 桶顶回来了：冲刺段没有退避，继续冲只会白烧设备，直接转尾段
                            P.end_head(order_sn, 'throttled')
                        # 让位：视频已经在限流，本单这会儿再占着归属只是空转，
                        # 放出去让同视频的下一条单去试，谁先吃到量算谁的。
                        B.video_release(aweme_id, order_sn)
                        show_message(f'第 {looper_num} 次 | {order_sn}', '视频限流',
                                     f'已入账 {result["delta"]} 条，剩余留单下一轮继续')

                show_message(f'第 {looper_num} 次 | {order_sn}', '处理用时',
                             f'{time.time() - start_time:.1f}s')
                del order
                print('\n')
            except Exception as exc:
                print(str(exc))
                try:
                    release_order(order_sn)
                except Exception:
                    pass
                # 异常退出也算一次让位：视频归属不放开，同视频的其他单会一直排队。
                try:
                    if _aweme:
                        B.video_release(_aweme, order_sn)
                except Exception:
                    pass
        del order_list
        gc.collect()
        # 这一页的单全在退避里（或者全被别人拿着）：把页让出去，下一轮重新领。
        # 不让页的话，一页全是退避单的窗口会整晚钉在这一页上，把 1/30 的并行度耗空。
        if live_candidates == 0:
            release_page(page)
        if did_work:
            # 干过活就立刻领下一页：换手速度就是这么来的，处理完一单马上接下一单，
            # 不在两单之间睡。页池会把它领到真正还有单的地方去。
            continue
        # 整页单子都被节奏层挡回来时，按最近的到点时间睡一觉。
        # 为什么要睡：加了曲线以后，一圈里大部分单子都是「未到点」，
        # 不睡就会以每秒十几圈的速度空打平台订单接口，既没意义又容易被限。
        now = time.time()
        soon = [t - now for t in list(pace_skip.values()) + list(zero_skip.values()) + list(day_skip.values())
                if t > now]
        # 一圈什么都没干：全单都在零增量/曲线/日额度退避里时按最近的到点时间醒，
        # 封顶一个整觉 IDLE_SLEEP（15 秒）。原来固定 5~60 秒，退避单把窗口按在原地，
        # 现在退避单不占窗口，整池都没活才该睡，而且只睡一个整觉就重新扫页。
        wait = IDLE_SLEEP if not soon else min(IDLE_SLEEP, max(1.0, min(soon)))
        # 页已经让出去了，这一觉可以放心睡满：别的窗口会接着吃这页上的单。
        time.sleep(wait)
        if DRY:
            show_message(f'第 {looper_num} 次', '演练结束', '一页跑完即退出，未改任何订单状态')
            raise SystemExit(0)
    except Exception as exc:
        print(str(exc))
