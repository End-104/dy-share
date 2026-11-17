# coding: utf-8
"""ayk_dy_share_handler.py —— 供应商分享量订单处理端（货号 643 / 1763）

与播放端 ayk_dy_bf_handler.py 完全同构，只把「播放量」换成「分享量」：
    播放端：get_play_count + ShareBooster(field='',  counter=get_play_count)
    分享端：share_count    + ShareBooster(field='share_delta', counter=share_count)

用法：
    python ayk_dy_share_handler.py cfg_qw_dy_share_1763 1 24         # 正常接单
    python ayk_dy_share_handler.py cfg_qw_dy_share_1763 1 24 dry     # 演练：只试打，不改订单
    python ayk_dy_share_handler.py cfg_qw_dy_share_643  1 24

第三个参数是每单线程数（省略=20），第四个参数是 dry 时进演练模式。
单个进程只处理一个货号的一页订单。货号 643 与 1763 各起若干进程，
页码错开，靠平台 execState 锁避免两个进程抢同一单。

每单流程（9 步，输入 -> 输出）：
    1  拉单       get_order_list('2')                      -> state=2 的订单数组
    2  读详情     get_order_detail(order_sn)               -> 订单对象，校验 state 仍为 2
    3  抢锁       execState?order_sn=..                    -> '1' 表示已被别的进程领走，跳过
    4  换 ID      execute?order_sn=..&exec_update=1         -> 19 位 aweme_id，失败置 state=3
    5  查额度     video_room(aweme_id)                     -> 今日剩余条数；<=5 直接把单排到次日零点
    6  读基数     share_count(aweme_id)                    -> 当前真实分享量；读不到退回 execNum
    7  算缺量     min(下单数量, 今日额度, 单轮切片)          -> 本轮目标；缺量 <=LOSE_NUM 直接完成
    8  上报       ShareBooster.run(day_cap=1800)           -> accepted/delta/day_capped
    9  回写结算   再读 share_count -> update_order_progress -> 完成(4) / 次日续刷 / 退款(7)

单视频每日口径（跨进程共享，台账在 used_devices.db）：
    同一条视频每个自然日最多入账 1800 条，下单量小于 1800 就按下单量；
    第二天零点自动重置，同一条视频可以再刷 1800 条，不需要人工清库。

多单并发时的调度口径（保证每个用户都能吃满自己的额度）：
    · SHARE_ROUND_CHUNK=400  每轮只吃 400 条就回队尾，避免前几单占满设备
    · SHARE_DEVICE_GAP=300   同一台设备两次分享最小间隔，防止被服务端判机器
    · SHARE_ZERO_ROUNDS=4    零增量最多退避重试 4 轮（600/1200/2400/3600s）才退款
"""

import gc
import os
import sys
import time

from dy_share import (DevicePool, InfoPool, ShareBooster, SHARE_FIELD,
                      VIDEO_DAY_CAP, devices34, infos, load_extra_devices,
                      load_extra_infos, resolve_aweme_id, share_count)
from function import FunctionApp, show_message, TIME_WAIT
import used_store as U
import pace_ledger as P
import order_baseline as B

function = FunctionApp()

LOSE_NUM = 5                # 余量：分享按真实增量结算，留 5 条收口，留大等于白刷
THREADS = int(sys.argv[3]) if len(sys.argv) > 3 and str(sys.argv[3]).isdigit() else 20
# 每单每轮最长耗时（秒）。原来是 2400，给「1800 条满额单一次吃完」留的余量；
# 现在节奏层不再限速，一轮吃到视频桶关窗或吃满本批 180 条就自然退出，
# 2400 秒只会让一条单占住一个窗口四十分钟，其余 11 个窗口排在同一单后面干等。
BUDGET = int(os.environ.get('SHARE_BUDGET', 60 * 20))
CHANNELS = ('1128', '32')   # 实测能承载 share_delta 的两条 Android 通道
# ---- 交付口径：客户下多少就刷多少，今天不齐明天接着刷
# 退款链路与播放端（D:\jiedan\ayk_dy_bf_handler.py）保持一致：本端不调用
# function.update_order_refund，不写状态 7，能写的只有 3（异常）和 4（完成）。
# 刷不动时只做退避重试与跨天等待，单子一直留在处理中；
# 要让客户退钱只有两条路：平台后台人工操作，或客户自己在平台申请（平台侧推进 5→6→7）。
# zero_rounds 退避链保留：它只负责「关窗时别烧设备」，与退单无关。
ZERO_BACKOFF = float(os.environ.get('SHARE_ZERO_BACKOFF', 600))   # 零增量退避基数（秒），每轮翻倍，封顶 3600

# ---- 平台天花板：本地台账的数字不是平台额度，实测天花板 341~639 的视频打到顶之后
# delta 会长时间归零，这不是死号也不是限流，是这条视频当天的记账预算用完了。
# 判到天花板只把单子挪到次日零点，不计数、不退单。
# CEIL_MIN_TODAY：今天至少吃到这么多条才允许判「当日天花板」并推到零点。
# 日限抬到 10 万以后，写死的 1000 会让「今天进过 1000 条、这一轮恰好零增量」的
# 普通关窗被误判成日顶，一判就跳过一整天。改成跟着日限走：日限 - 800。
# 日限仍是 1800 的老配置算出来还是 1000（行为不变），日限 10 万时是 99200（永不到点）。
CEIL_MIN_TODAY = int(os.environ.get('SHARE_CEIL_MIN', 0)) or max(1000, int(VIDEO_DAY_CAP) - 800)

# ---- 订单级互斥：一条单子同一时刻只允许一个窗口发上报
# 三方接口的 execState 不是会话语义：execute 拿回来的 1 只代表「这一次领取」，
# 下一秒再查又回 2。12 个窗口因此会同时对着同一条单子各发一批，实测把 800 条
# 的单子刷到 1100（超交 300，等于白送）。平台侧没有可用的订单锁，只能在本机做：
# 谁抢到 order_<sn>.lock 谁刷，抢不到的直接跳过，等下一圈再抢。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ORDER_LOCK_TTL = float(os.environ.get('SHARE_ORDER_LOCK_TTL', BUDGET + 600))


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


def ceiling_hit(aweme_id: str, result: dict) -> bool:
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
    if used < CEIL_MIN_TODAY:
        return False
    if U.video_room(aweme_id, VIDEO_DAY_CAP) <= LOSE_NUM:
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

# 同一批窗口吃的货号：1763 和 643 都是「分享量」类，配置里默认只挂一个，
# 结果一半窗口在别的货号上永远空转。这里用逗号把要吃的货号全列出来，
# 每个窗口每圈把每个货号的单子都拉一遍再合并，谁的货号都能被抢到。
_raw_goods = os.environ.get('SHARE_GOODS') or ''
GOODS_LIST = [g.strip() for g in _raw_goods.split(',') if g.strip()]
if not GOODS_LIST:
    GOODS_LIST = [str(getattr(function, 'goods_sn', '') or '').strip()]
GOODS_LIST = [g for g in GOODS_LIST if g]
if not GOODS_LIST:
    GOODS_LIST = ['1763']

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


def order_link(order) -> str:
    """从订单 params 里取作品链接。params 是 [{alias,name,value}] 数组。"""
    for p in order.get('params') or []:
        value = str(p.get('value') or '')
        if 'douyin.com' in value or value.isdigit():
            return value
    return ''


def run_booster(aweme_id: str, target: int) -> dict:
    """分享通道上报。field 用已确认的 share_delta，计数器用官方 share_count。

    day_cap 传的是本地单视频每日上限（SHARE_DAY_CAP，默认 10 万，等于不设限）。
    为什么还传：booster 内部用它在「预留名额 / 收工复核 / 台账校准」三处对账，
    传 0 会被 day_cap_hit 判成恒真（used >= 0），所以只能给大数字，不能给 0。
    """
    booster = ShareBooster(aweme_id, target, threads=THREADS, timeout=BUDGET,
                           channels=CHANNELS, verbose=True, field=SHARE_FIELD,
                           counter=share_count, day_cap=VIDEO_DAY_CAP,
                           device_pool=DEVICE_POOL, info_pool=INFO_POOL)
    return booster.run()


def current_share(order_sn: str, aweme_id: str, start_num: int) -> int:
    """读当前分享量；三方接口挂了就用平台记录的 execNum 折算，别把单挂死。"""
    value = share_count(aweme_id)
    if value >= 0:
        return value
    try:
        return start_num + int(function.upload_server_content(f'execNum?order_sn={order_sn}'))
    except Exception:
        return start_num


# ---- 自动接单：把平台挂在「已付款(1)」的分享单自己推进到「处理中(2)」
# 为什么要在本机做：播放标品是平台自家货源，客户付完款 1~5 秒平台就自动置 2；
# 1763 / 643 这种自发货源平台不给自动接单开关，单子会一直停在 1 等人点。
# 实测 3249467、3247753 挂在 1 上几个小时没人碰，客户那边看到的就是「没开工」。
# 接单只是把 1 写成 2，不动数量、不动金额、不回写任何执行数据，剩下的全交给缺量循环。
AUTO_ACCEPT = str(os.environ.get('SHARE_AUTO_ACCEPT', '1')).lower() not in ('0', 'false', 'no', 'off')
# 每圈最多接几张：一次把几百张全置 2，会让所有窗口同时去抢同一批单的锁，
# 抢不到的原地空转；30 张一批，够 12 个窗口分工，也不会把列表撑爆。
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
            msg = function.update_order_state(sn, 2, '自动接单')
            order['state'] = 2
            pulled.append(order)
            show_message(f'第 {looper_num} 次 | {sn}', '自动接单',
                         f'货号 {_goods} | 下单 {order.get("number")} 条 | '
                         f'已置处理中 | {str(msg)[:60]}')
    return pulled


while True:
    try:
        looper_num += 1
        # 一个窗口吃多个货号：各自拉一遍再合并，单货号时行为与原来完全一致
        order_list = []
        for _goods in GOODS_LIST:
            _part = function.get_order_list('2', goods_sn=_goods)
            if _part:
                order_list.extend(_part)
        # 接单放在拉列表之后、统计数量之前：这一圈新接进来的单直接进本次队列，
        # 不用等下一圈（一圈可能二十分钟）。dry 模式不改平台状态，跳过。
        if AUTO_ACCEPT and not DRY:
            order_list.extend(promote_paid(looper_num))
        # 订单数量这行不再每圈打：数量没变就 30 秒一次，数量一变立刻打。
        # 每圈都打会在全单入退避时刷成每秒好几行，真看日志时反而找不到有用的行。
        if len(order_list) != last_list_n or (time.time() - last_list_ts) >= LIST_LOG_EVERY:
            show_message(f'第 {looper_num} 次', f'订单数量({"+".join(GOODS_LIST)})', str(len(order_list)))
            last_list_n = len(order_list)
            last_list_ts = time.time()
        if len(order_list) == 0:
            page = int(time.time() * 1000) % 10
            for _goods in GOODS_LIST:
                _part = function.get_order_handler(page, goods_sn=_goods)
                if _part:
                    order_list.extend(_part)
            if len(order_list) == 0:
                del order_list
                gc.collect()
                time.sleep(TIME_WAIT)
                continue
            show_message(f'第 {looper_num} 次', f'订单页:{page} | 订单数量', str(len(order_list)))
        order_list.reverse()

        did_work = False           # 本页有没有真正发出过上报，用来决定这一圈要不要睡

        for order in order_list:
            try:
                start_time = time.time()
                order_sn = str(order['orderSN'])
                order_num = int(order['number'])
                start_num = int(order['startNum'])

                # 这条视频今天的额度已经用完：留单不动，明天零点后自动接着跑
                if day_skip.get(order_sn, 0) > time.time():
                    continue
                # 上一轮零增量：退避期内不碰，避免持续烧设备
                if zero_skip.get(order_sn, 0) > time.time():
                    continue
                # 曲线还没走到这条单的额度：跳过，别空转
                if pace_skip.get(order_sn, 0) > time.time():
                    continue

                # ---- 演练：只解析链接试打，不抢锁、不回写、不改状态
                if DRY:
                    aweme_id = resolve_aweme_id(order_link(order))
                    if len(aweme_id) != 19:
                        show_message(f'第 {looper_num} 次 | {order_sn}', '演练异常', '取不到视频ID')
                        del order
                        continue
                    room = U.video_room(aweme_id, VIDEO_DAY_CAP)
                    if room <= 0:
                        show_message(f'第 {looper_num} 次 | {order_sn}', '演练跳过',
                                     f'视频 {aweme_id} 今日额度已用满')
                        del order
                        continue
                    before = share_count(aweme_id)
                    result = run_booster(aweme_id, min(3, room))
                    show_message(f'第 {looper_num} 次 | {order_sn}', '演练结果',
                                 f'{aweme_id} | 前:{before} | 后:{result["final"]} | '
                                 f'增量:{result["delta"]} | 接受:{result["accepted"]} | '
                                 f'通道:{result["channels"]} | 原因:{result["reason"] or "-"}')
                    del order
                    continue

                order = function.get_order_detail(order_sn)
                if int(order['state']) != 2:
                    show_message(f'第 {looper_num} 次 | {order_sn}', '状态异常', '跳过')
                    del order
                    continue

                # 平台侧占位锁：返回 1 说明别的进程已经在跑这单
                if int(function.upload_server_content(f'execState?order_sn={order_sn}')) == 1:
                    show_message(f'第 {looper_num} 次 | {order_sn}', '已被领取', '跳过')
                    del order
                    continue

                aweme_id = function.upload_server_content(f'execute?order_sn={order_sn}&exec_update=1')
                if len(aweme_id) != 19:
                    aweme_id = resolve_aweme_id(order_link(order))
                if len(aweme_id) != 19:
                    show_message(f'第 {looper_num} 次 | {order_sn}', '订单异常',
                                 function.update_order_state(order_sn, 3, 'aweme_id错误'))
                    del order
                    continue

                # 单视频每日上限前置检查：额度已经用满就不必读计数、不必开线程，
                # 直接把单子排到明天零点，省一次网络往返也省一批设备。
                room = U.video_room(aweme_id, VIDEO_DAY_CAP)
                if room <= LOSE_NUM:
                    day_skip[order_sn] = U.next_day_start()
                    show_message(f'第 {looper_num} 次 | {order_sn}', '当日额度已用尽',
                                 f'视频 {aweme_id} 今日已入账 {U.video_day_used(aweme_id)}'
                                 f'/{VIDEO_DAY_CAP}，明日 00:00 后自动续刷')
                    del order
                    continue

                current = current_share(order_sn, aweme_id, start_num)
                # startNum=0 不是「起点是 0」，是这个货号的平台不下发基线（1763/643 实测全 0）。
                # 拿 0 直接算 净增 = 绝对值 - 0，等于把视频历史总分享量算成本次交付：
                # 3252608（下 100 条、视频历史 500）第一轮就 100-500=-400 <= 5，40 秒写 4 完成，
                # 一条都没发。所以这里先补基线：本地台账优先，没有就用此刻的绝对值打基线并回写平台。
                start_num = B.ensure(order_sn, aweme_id, start_num)
                if start_num <= 0 and current >= 0:
                    start_num = B.put(order_sn, aweme_id, current)
                    if start_num > 0:
                        function.update_order_progress(order_sn, start_num, start_num)
                        show_message(f'第 {looper_num} 次 | {order_sn}', '基线落地',
                                     f'平台 startNum=0 → 本地基线 {start_num}，缺量按本地算')
                surplus_num = order_num - (current - start_num)
                if surplus_num <= LOSE_NUM:
                    show_message(f'第 {looper_num} 次 | {order_sn}', '订单完成',
                                 function.update_order_state(order_sn, 4, '订单完成'))
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
                target = min(target, surplus_num, room)
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
                # 为什么必须在本机锁：12 个窗口都会拿到同一个 execState=2，
                # 各算一次缺量各发一批，缺量是各算各的，超出来的量没人负责。
                if not claim_order(order_sn):
                    # 同一条单在 24 个窗口里被跳过是常态，按单节流，不然日志全是这行
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
                    if live > current:
                        current = live
                        rec = P.align(order_sn, current - start_num)
                    surplus_num = order_num - (current - start_num)
                    # 硬夹：本轮目标永远不超过「下单量 - 已净增」，也不超过视频当日剩余额度
                    target = min(target, max(0, surplus_num), room)
                if surplus_num <= LOSE_NUM:
                    show_message(f'第 {looper_num} 次 | {order_sn}', '订单完成',
                                 f'净增 {current - start_num}/{order_num}，'
                                 + str(function.update_order_state(order_sn, 4, '订单完成')))
                    zero_rounds.pop(order_sn, None)
                    zero_skip.pop(order_sn, None)
                    day_skip.pop(order_sn, None)
                    pace_skip.pop(order_sn, None)
                    release_order(order_sn)
                    del order
                    continue
                if target <= 0:
                    show_message(f'第 {looper_num} 次 | {order_sn}', '本轮不用刷',
                                 f'当前:{current} | 缺量:{surplus_num} | 额度:{room}')
                    release_order(order_sn)
                    del order
                    continue

                phase = '冲刺' if P.in_head(rec) else '尾段'
                show_message(f'第 {looper_num} 次 | {order_sn}', f'开始上报[{phase}]',
                             f'视频ID:{aweme_id} | 当前:{current} | 缺量:{surplus_num} | '
                             f'今日额度:{room} | 应达:{P.due(rec)} | 本轮目标:{target}')

                result = run_booster(aweme_id, target)
                did_work = True
                show_message(f'第 {looper_num} 次 | {order_sn}', '上报完成',
                             f'接受:{result["accepted"]} | 尝试:{result["attempts"]} | '
                             f'真实增量:{result["delta"]} | 通道:{result["channels"]} | '
                             f'中止原因:{result["reason"] or "-"}')

                function.upload_server_content(
                    f'update?order_sn={order_sn}&exec_state=1&exec_num={result["delta"]}')

                final = share_count(aweme_id)
                if final < 0:
                    show_message(f'第 {looper_num} 次 | {order_sn}', '订单异常',
                                 function.update_order_state(order_sn, 3, '获取分享量错误'))
                    release_order(order_sn)
                    del order
                    continue

                exec_num = final - start_num
                left = order_num - exec_num
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
                    show_message(f'第 {looper_num} 次 | {order_sn}', '订单完成',
                                 function.update_order_state(order_sn, 4, '订单完成'))
                    zero_rounds.pop(order_sn, None)
                    zero_skip.pop(order_sn, None)
                    day_skip.pop(order_sn, None)
                    pace_skip.pop(order_sn, None)
                elif result.get('day_capped') or U.video_room(aweme_id, VIDEO_DAY_CAP) <= LOSE_NUM:
                    # 当日额度用尽不是故障：订单留着，明天同一视频还能再刷 1800 条。
                    # 这里除了看 run() 的 day_capped 标记，还回查一次台账，避免并发
                    # 进程把这条视频的额度吃掉之后本进程还傻等着。
                    day_skip[order_sn] = U.next_day_start()
                    zero_rounds[order_sn] = 0
                    zero_skip.pop(order_sn, None)
                    show_message(f'第 {looper_num} 次 | {order_sn}', '当日额度用尽',
                                 f'视频 {aweme_id} 今日已入账 {U.video_day_used(aweme_id)} 条'
                                 f'（上限 {VIDEO_DAY_CAP}），缺量 {left} 留到明日继续')
                elif result['delta'] <= 0 and ceiling_hit(aweme_id, result):
                    # 平台天花板：今天进过量、这轮零增量、本地额度还没满。
                    # 留单到零点，不计天数、不退单 —— 与播放端一致，缺量明天接着吃。
                    # 为什么还要 end_head：冲刺段没有退避，桶已经关了还按 180 一批
                    # 猛打就是纯烧设备；转尾段后小时闸会把节奏压到回补速率。
                    if P.in_head(rec):
                        P.end_head(order_sn, 'ceiling')
                    day_skip[order_sn] = U.next_day_start()
                    zero_rounds[order_sn] = 0
                    zero_skip.pop(order_sn, None)
                    show_message(f'第 {looper_num} 次 | {order_sn}', '当日天花板',
                                 f'今日入账 {U.video_day_used(aweme_id)} 条即停 | '
                                 f'缺量 {left} 留到明日 | 不退单，等次日窗口重开')
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
                    # 600/1200/2400/3600 封顶：纯退避。轮数只用于日志和 3600 秒上限，
                    # 攒到多少轮都不退单，和播放端「一直重试」是同一套语义。
                    backoff = min(ZERO_BACKOFF * (2 ** (rounds - 1)), 3600)
                    zero_skip[order_sn] = time.time() + backoff
                    # 开头时间只记第一次，中间零星吃到几条会清空重算，避免把间断的零增量
                    # 攒成一段「连续挂死」
                    zero_since.setdefault(order_sn, time.time())
                    stuck_h = (time.time() - zero_since[order_sn]) / 3600.0
                    show_message(f'第 {looper_num} 次 | {order_sn}', '零增量',
                                 f'连续 {rounds} 轮 | 已挂 {stuck_h:.1f}h | '
                                 f'{backoff}s 后重试 | 留单不退 | '
                                 f'原因:{result["reason"] or "-"}')
                else:
                    zero_rounds[order_sn] = 0
                    zero_skip.pop(order_sn, None)
                    zero_since.pop(order_sn, None)
                    if result['throttled']:
                        if P.in_head(rec):
                            # 桶顶回来了：冲刺段没有退避，继续冲只会白烧设备，直接转尾段
                            P.end_head(order_sn, 'throttled')
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
        del order_list
        gc.collect()
        # 整页单子都被节奏层挡回来时，按最近的到点时间睡一觉。
        # 为什么要睡：加了曲线以后，一圈里大部分单子都是「未到点」，
        # 不睡就会以每秒十几圈的速度空打平台订单接口，既没意义又容易被限。
        now = time.time()
        soon = [t - now for t in list(pace_skip.values()) + list(zero_skip.values()) + list(day_skip.values())
                if t > now]
        # 一圈什么都没干就至少睡 5 秒：全单都在零增量/曲线/日额度退避里时，
        # 原来 soon 为空会一脚不睡地空转，一秒能转两圈，日志和平台接口一起被灌满。
        if not did_work:
            time.sleep(5.0 if not soon else min(60.0, max(5.0, min(soon))))
        if DRY:
            show_message(f'第 {looper_num} 次', '演练结束', '一页跑完即退出，未改任何订单状态')
            raise SystemExit(0)
    except Exception as exc:
        print(str(exc))
