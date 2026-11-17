# coding: utf-8
"""pace_ledger.py —— 订单节奏账本：前段冲刺 + 后段长尾

两段式，为什么不是一段匀速：
    实测（channel_cap.jsonl 2026-11-16 00:13）1128@api26 一条通道 40 次尝试
    1 分钟内进 16 条；32 通道 60 次尝试一次进 26 条。也就是单窗口能吞 220 上下。
    下单后前 20~40 分钟是桶最新鲜的时候，这时候匀速只发 6 条/批纯属浪费；
    而窗口关掉之后一小时只回补 5~9 条，再怎么冲也冲不动。
    所以正确的形状是：前段有多少吃多少（head），后段按回补速率铺（tail）。

    head 段：从接单开始 head_min 分钟内，每批 head_chunk 条全力打，
             上限 head_pct × total，不打小时闸、不打最小批量闸。
             打满 head_target、或桶关窗（handler 调 end_head）、或时间窗到点，
             三者任一发生就转 tail。
    tail 段：剩余量按曲线铺到 duration 结束，每小时上限 = 尾段斜率 × hourly_tol，
             追欠账也不许突破这道闸，否则关窗攒的量会在开窗后一小时内爆掉。

算术（1800 条、默认参数 12h / head 35% / head 30min）：
    head 目标 630 条，30 分钟内打完 → 客户下单半小时看到 600 上下
    实际受桶限制可能只到 200~400，缺口由 tail 段按 195 条/小时追平
    tail 剩 1170 条铺 11.5 小时 ≈ 102 条/小时

状态文件 pace_state.json，每个 order_sn 一条：
    total / start_num / start_ts / duration / shape   订单与曲线基本参数
    head_pct / head_end                              冲刺段份额与截止时刻
    delivered / day_log / hour_log                   真实入账台账
    per_day / hourly_tol                             日闸与小时闸
    lag / ticks                                     丢弃的欠账、批次数

命令行：
    python pace_ledger.py list                          # 所有单子的进度
    python pace_ledger.py show ORDER_SN                 # 单子明细
    python pace_ledger.py sim ORDER_SN --hours 12       # 打印分钟级曲线采样
    python pace_ledger.py reset ORDER_SN                # 删掉一条，重下单用
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, 'pace_state.json')
SHAPES = ('linear', 'front', 'back', 'sip')

DEFAULT_HOURS = 12.0
DEFAULT_SHAPE = 'linear'
DEFAULT_PER_DAY = 0             # 0 = 不受日配额限制，只受 duration 与 DAY_CAP 限制
DEFAULT_HOURLY_TOL = 1.3        # 尾段追账时每小时最多跑到尾段斜率的 1.3 倍
DEFAULT_HEAD_PCT = 0.35         # 冲刺段吃下单量的多少
DEFAULT_HEAD_MIN = 30.0         # 冲刺段持续多少分钟
DEFAULT_HEAD_CHUNK = 180        # 冲刺段单批上限，与 daily1800 的 WINDOW_CHUNK 一致
DEFAULT_HEAD_FLOOR = 220        # 冲刺段的最低目标：单窗口实测认账 220 上下
DEFAULT_MIN_RATE = 60           # 尾段最低铺设速率（条/小时），剩余量少时压缩尾段时长
DEFAULT_MAX_RATE = 130          # 尾段常态最高速率（条/小时），防爆量
DEFAULT_BURST_RATE = 220        # 按剩余时长追不上时允许抬到的上限 = 单窗口实测容量
DEFAULT_HEAD_RETRY = 600.0      # 冲刺被零增量打断后，第一次重开间隔（秒），之后翻倍
DEFAULT_HEAD_RETRIES = 6        # 一段内最多重开几次冲刺（600/1200/2400/3600…覆盖约 3 小时关窗）


# ---------------------------------------------------------------- 跨进程互斥
LOCK_FILE = STATE_FILE + '.lock'


@contextlib.contextmanager
def _lock(timeout: float = 10.0):
    """账本读改写必须串行：12 个接单窗口同时 note 会互相覆盖，delivered 一次涨几百。

    用 O_EXCL 建锁文件，抢不到就退避重试；持锁超过 timeout*6 视为残留锁（进程被
    kill 不会自己清），直接删掉重建。超过 timeout 仍拿不到就放行，宁可偶发争一次，
    也不能把接单主循环卡死。
    """
    fd = None
    end = time.time() + timeout
    while True:
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            break
        except FileExistsError:
            if time.time() > end:
                try:
                    if time.time() - os.path.getmtime(LOCK_FILE) > timeout * 6:
                        os.remove(LOCK_FILE)
                except Exception:
                    pass
                yield
                return
            time.sleep(0.01 + random.random() * 0.02)
    try:
        yield
    finally:
        try:
            os.close(fd)
        except Exception:
            pass
        try:
            os.remove(LOCK_FILE)
        except Exception:
            pass


# ---------------------------------------------------------------- 落盘
def _load() -> dict:
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict):
    """先写临时文件再替换，避免进程被 kill 时留下半个 JSON。

    为什么必须原子写：这个账本被多个订单进程共享，写坏一次后面所有单子的
    due 都算不出来，等于调度失灵。
    """
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def _today(ts: float = None) -> str:
    return time.strftime('%Y-%m-%d', time.localtime(ts or time.time()))


def _hour_key(ts: float = None) -> str:
    return time.strftime('%Y-%m-%d %H', time.localtime(ts or time.time()))


# ---------------------------------------------------------------- 自然日分段
def _day0(ts: float) -> float:
    """ts 所属自然日的零点。整段逻辑里只允许这一处算零点。

    为什么不直接 ts - ts % 86400：那算的是 UTC 零点，东八区会差 8 小时，
    额度重置时刻跟着一起错位。
    """
    lt = time.localtime(ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _roll(rec: dict, now: float = None) -> bool:
    """把账本推进到「当前自然日段」，返回是否改动过。

    段 = [seg_start, seg_start + seg_dur)，且不跨零点：
        首次登记 seg_start = 单子开始时刻
        跨零点后 seg_start = 当日零点

    seg_base 记段起点的累计入账，seg_total 记这一段的目标量。
    为什么要这三个字段：due / allowance / hourly_quota 全部改成按「今天还能刷多少」
    算之后，跨零点那次推进会自然重开一段冲刺，把新一天的配额先吃掉；
    不改的话昨天冲满 35% 之后今天 in_head() 永远返回 False，一跳过冲刺
    就只剩尾段一百条/小时，新一天的额度会被昨天的时间轴压死在半路。
    """
    now = now or time.time()
    dur = float(rec.get('duration') or DEFAULT_HOURS * 3600)
    seg_start_old = float(rec.get('seg_start') or rec.get('start_ts') or now)
    seg_dur_old = float(rec.get('seg_dur') or dur)
    seg_end = seg_start_old + seg_dur_old
    day_changed = bool(rec.get('seg_start')) and _today(now) != _today(seg_start_old)
    if rec.get('seg_total') and now < seg_end and not day_changed:
        return False                                   # 还在本段内、也没跨日，不动
    total = int(rec.get('total') or 0)
    delivered = int(rec.get('delivered') or 0)
    if total - delivered <= 0:
        return False
    per_day = int(rec.get('per_day') or 0)
    # 整单截止时刻 = 接单时刻 + duration，跨日重开段也不许往后推：
    # 推了就等于每过一个零点给自己续一天，23 点接的单永远跑不完。
    # 真的超期了（平台关窗太久）才按 now + duration 续一段，属于认账后的继续推进。
    deadline = float(rec.get('start_ts') or now) + dur
    if rec.get('seg_start'):
        new_start = max(now, _day0(now))               # 跨日：从当下或零点起算
    else:
        new_start = float(rec.get('start_ts') or now)  # 首次：从单子开始时刻起算
    if deadline <= new_start:
        deadline = new_start + dur
    new_end = min(new_start + dur, deadline)           # 不再按零点截断，避免把尾巴压成爆量
    if new_end - new_start < 60:
        new_end = new_start + 60
    rec['seg_start'] = new_start
    rec['seg_base'] = delivered
    rec['seg_total'] = total if not per_day else min(total, delivered + per_day)
    rec['seg_dur'] = new_end - new_start
    rec['head_end'] = min(new_end, new_start
                          + float(rec.get('head_min') or DEFAULT_HEAD_MIN) * 60.0)
    rec['head_end_reason'] = 'roll'
    rec['head_pause'] = 0.0          # 新段不继承上一段的暂停与重开次数
    rec['head_retries'] = 0
    return True


# ---------------------------------------------------------------- 基础量
def head_target(rec: dict) -> int:
    """本段冲刺目标。有分段信息就按段算（今天剩余量的 head_pct），
    老账本没有 seg_total 时回落整单口径，保证升级过程中行为不跳变。

    为什么要给一个下限 head_floor：跨日重开段时剩余量可能只剩两百来条，
    按 35% 算只冲 70，剩下的靠尾段每小时十几条爬——平台零点明明开了一个
    完整的窗口（实测认账 220 上下），却被我们自己的曲线拖了一整天。
    所以冲刺目标取 max(剩余 × head_pct, min(单窗口容量, 剩余))：
    剩下的量本来就塞得进一个窗口时，全部当成冲刺量，当天就能收口。
    """
    if not rec.get('seg_total'):
        return int(round(int(rec.get('total') or 0) * float(rec.get('head_pct') or 0)))
    base = int(rec.get('seg_base') or 0)
    seg_total = int(rec.get('seg_total') or 0)
    left = max(0, seg_total - base)
    if left <= 0:
        return base
    pct = max(0.0, min(1.0, float(rec.get('head_pct') or 0)))
    take = max(int(round(left * pct)), min(int(DEFAULT_HEAD_FLOOR), left))
    return base + min(left, take)


def in_head(rec: dict, now: float = None) -> bool:
    """是否还在本段冲刺段。head_end 被 end_head 提前改写时立刻转尾段，
    head_pause 是「打断后暂停」：暂停期内算尾段，到点自动重开冲刺。"""
    now = now or time.time()
    if now < float(rec.get('head_pause') or 0):
        return False
    total = int(rec.get('total') or 0)
    if total <= 0:
        return False
    if int(rec.get('delivered') or 0) >= head_target(rec):
        return False
    return now < float(rec.get('head_end') or 0)


def _tail_sec(rec: dict, now: float = None) -> float:
    """本段尾段铺多少秒。取「段内可用时长」与「按最低速率铺完剩余所需时长」的小值。

    为什么要有这个下限：跨日重开段时剩余可能只剩两百来条，按 12 小时铺完
    就是十几条/小时——平台零点明明开了一个完整窗口，我们却用一整天去接。
    按 DEFAULT_MIN_RATE 压缩之后，小尾巴一两个小时就能收口，
    而整单首次那 1170 条尾巴仍然按 11.5 小时铺，Day1 的曲线一点不变。
    """
    seg_dur = float(rec.get('seg_dur') or rec.get('duration') or DEFAULT_HOURS * 3600)
    seg_start = float(rec.get('seg_start') or rec.get('start_ts') or (now or time.time()))
    head_end = float(rec.get('head_end') or seg_start)
    room_sec = max(60.0, seg_dur - max(0.0, head_end - seg_start))
    seg_total = int(rec.get('seg_total') or rec.get('total') or 0)
    tail_total = max(0, seg_total - head_target(rec))
    rate = float(DEFAULT_MIN_RATE)
    if tail_total <= 0 or rate <= 0:
        return room_sec
    return max(1800.0, min(room_sec, tail_total / rate * 3600.0))


def hourly_quota(rec: dict, now: float = None) -> int:
    """尾段每小时最多入账多少条（一律按本段时长算）。

    取三者的大头再封顶：
        基准 = 本段尾段总量 / 本段尾段时长   （按曲线该走的速率）
        追账 = 本段剩余量 / 本段剩余时长      （落后时自动抬高，保证能按时收口）
        封顶 = 基准 × 3                       （再急也不许一小时干三小时的量）

    为什么要有封顶：只按「剩余量/剩余时间」算，窗口连关几小时后，
    剩余时长会缩到一两个小时，配额会飙到几百条/小时，等于把攒下的欠账
    在一小时内爆完，前段做的平滑全白费。3 倍是实测能接受的上限
    （daily1800 单窗口一次认 220 条，一小时三批就是 660）。
    """
    now = now or time.time()
    head = head_target(rec)
    seg_total = int(rec.get('seg_total') or rec.get('total') or 0)
    tail_total = max(0, seg_total - head)
    if tail_total <= 0:
        return 10 ** 9
    seg_dur = float(rec.get('seg_dur') or rec.get('duration') or DEFAULT_HOURS * 3600)
    seg_start = float(rec.get('seg_start') or rec.get('start_ts') or now)
    tail_sec = _tail_sec(rec, now)
    tol = float(rec.get('hourly_tol') or DEFAULT_HOURLY_TOL)
    base = tail_total / tail_sec * 3600.0
    left = max(0, seg_total - int(rec.get('delivered') or 0))
    left_sec = max(600.0, seg_start + seg_dur - now)
    chase = left / left_sec * 3600.0
    # 封顶两道：先按尾段斜率 3 倍封，再按 DEFAULT_MAX_RATE 绝对封。
    # 为什么需要绝对封顶：段被零点截短后（20:00 接的单只剩 4 小时），
    # 尾段斜率会算成 334 条/小时，四个小时就想把一千多条灌进去——
    # 平台一天才给 639，多出来全是白烧的设备。130 条/小时是按实测日天花板
    # 639 除以有效跑量时长估出来的，够 Day1 的 102 条/小时用，压得住截短后的爆量。
    quota = min(max(base, chase) * tol, base * 3.0)
    # 追不上就抬一档，但只抬到单窗口实测容量为止：
    # 窗口连关几小时后剩余时长会缩到十分钟，纯按「剩余/剩余时长」算能算到几千条/小时，
    # 那不是节奏而是爆量；220 是实测单窗口认账量，一小时吃满一个窗口已经是上限。
    need = left / left_sec * 3600.0 if left_sec > 0 else 0.0
    cap = DEFAULT_MAX_RATE
    if need > cap:
        cap = min(float(DEFAULT_BURST_RATE), need)
    return max(1, int(round(min(quota, cap))))


def hour_used(rec: dict, now: float = None) -> int:
    return int((rec.get('hour_log') or {}).get(_hour_key(now)) or 0)


# ---------------------------------------------------------------- 曲线
def progress(shape: str, t: float) -> float:
    """t 是 0~1 的归一化时间，返回 0~1 的归一化进度。"""
    t = min(1.0, max(0.0, t))
    if shape == 'front':                 # 前重后轻
        return 1.0 - (1.0 - t) ** 2
    if shape == 'back':                  # 后重前轻
        return t ** 2
    return t                             # linear / sip 匀速


def due(rec: dict, now: float = None) -> int:
    """此刻累计应该涨到多少条（与 delivered 同单位，不含 start_num 基数）。

    冲刺段直接返回 head_target：意思是「这些额度此刻全部可用」，
    不是「这些已经该到账」。调度器拿它算可发量，看板上显示同一列。
    跨日之后 head_target 是按新一天的 seg_base 重算的，所以零点一过
    due 会重新抬起来，调度器自然把新一天的额度先吃掉。
    """
    now = now or time.time()
    total = int(rec.get('total') or 0)
    head = head_target(rec)
    delivered = int(rec.get('delivered') or 0)
    head_end = float(rec.get('head_end') or 0)
    if now < head_end and delivered < head:
        return min(head, total)
    seg_total = int(rec.get('seg_total') or total)
    tail_sec = _tail_sec(rec, now)
    elapsed = max(0.0, now - head_end)
    tail_total = max(0, seg_total - head)
    return min(total, head + int(round(
        tail_total * progress(rec.get('shape') or DEFAULT_SHAPE, elapsed / tail_sec))))


def allowance(rec: dict, now: float = None, chunk: int = 40, min_batch: int = 6,
              jitter: float = 0.35, head_chunk: int = DEFAULT_HEAD_CHUNK) -> int:
    """本轮最多给多少条。返回 0 表示这单此刻不该动。

    冲刺段：直接给 head_chunk，四道闸全不生效。理由是这个阶段桶正开着，
            时间就是产量，任何限速都等于把窗口白让出去。
    尾段：四道闸全开
        1 曲线闸：due - delivered
        2 小时闸：hourly_quota - 本小时已入账（压住关窗攒下的欠账）
        3 日配额闸：per_day - 今日已入账
        4 批量闸：不超过 chunk，低于 min_batch 就跳过
    """
    now = now or time.time()
    total = int(rec.get('total') or 0)
    seg_total = int(rec.get('seg_total') or total)
    delivered = int(rec.get('delivered') or 0)
    if total <= 0 or delivered >= total or delivered >= seg_total:
        return 0

    if in_head(rec, now):
        # 冲刺段四道闸全不生效，但仍不能越本段目标与整单目标。
        return max(1, min(int(head_chunk), head_target(rec) - delivered,
                          seg_total - delivered, total - delivered))

    ahead = due(rec, now) - delivered
    if ahead <= 0:
        return 0
    ahead = min(ahead, hourly_quota(rec, now) - hour_used(rec, now))
    if ahead <= 0:
        return 0
    per_day = int(rec.get('per_day') or 0)
    if per_day > 0:
        used = int((rec.get('day_log') or {}).get(_today(now)) or 0)
        ahead = min(ahead, per_day - used)
        if ahead <= 0:
            return 0
    n = min(ahead, int(chunk), seg_total - delivered, total - delivered)
    if n < int(min_batch):
        return 0
    if jitter > 0:
        # 抖动：固定批大小会打出完全等间隔的台阶，比匀速更像脚本。
        # 用 order_sn + 分钟数做种子，同一分钟内多次调用给同一个值，可复现。
        seed = hash((rec.get('order_sn') or '', int(now // 60))) & 0xFFFFFFFF
        rnd = random.Random(seed)
        n = int(round(n * rnd.uniform(1.0 - jitter, 1.0 + jitter)))
        n = max(int(min_batch), min(n, int(chunk), seg_total - delivered, total - delivered))
    return n


def overdue(rec: dict, now: float = None, chunk: int = 40) -> int:
    """欠账条数：曲线要求已经涨到、但因为关窗没吃到的部分。只用于统计与告警。"""
    now = now or time.time()
    return max(0, due(rec, now) - int(rec.get('delivered') or 0) - int(chunk))


def next_check(rec: dict, now: float = None, need: int = 1) -> float:
    """还差 need 条到点的时刻。冲刺段返回 now：不睡，接着吃。

    为什么用二分找而不是闭式反解：曲线形状有三种、账本又按自然日切段，
    闭式反解要分六种情况写，改一处就得同步六处；这里直接对 due() 求逆，
    24 次二分的精度已经到毫秒级，形状和分段怎么改都不用动这个函数。
    """
    now = now or time.time()
    if in_head(rec, now):
        return now
    want = int(rec.get('delivered') or 0) + max(1, int(need))
    seg_dur = float(rec.get('seg_dur') or rec.get('duration') or DEFAULT_HOURS * 3600)
    lo, hi = 0.0, max(60.0, seg_dur)
    if due(rec, now + hi) < want:
        return now + hi                     # 本段内到不了，到点再来重算
    for _ in range(24):
        mid = (lo + hi) / 2.0
        if due(rec, now + mid) >= want:
            hi = mid
        else:
            lo = mid
    return now + hi


# ---------------------------------------------------------------- 对外接口
def _ensure_raw(order_sn: str, total: int, start_num: int = 0,
           hours: float = DEFAULT_HOURS, shape: str = DEFAULT_SHAPE,
           per_day: int = DEFAULT_PER_DAY, head_pct: float = DEFAULT_HEAD_PCT,
           head_min: float = DEFAULT_HEAD_MIN, restart: bool = False) -> dict:
    """登记一条单子；已存在就直接返回，不动已跑的进度。

    为什么不覆盖已有记录：订单进程会被 kill 重启、平台也会把单子重新下发，
    每次重启都重置 start_ts 会让曲线永远从头开始，单子永远跑不完。
    """
    data = _load()
    rec = data.get(order_sn)
    if rec and not restart:
        changed = False
        if 'hour_log' not in rec:                    # 老账本升级：补小时桶与新增字段
            rec['hour_log'] = {}
            changed = True
        if 'hourly_tol' not in rec:
            rec['hourly_tol'] = float(DEFAULT_HOURLY_TOL)
            changed = True
        if 'head_min' not in rec:
            rec['head_min'] = float(head_min)
            changed = True
        if 'head_pct' not in rec:
            rec['head_pct'] = float(head_pct)
            changed = True
        if 'head_end' not in rec:
            rec['head_end'] = (float(rec.get('start_ts') or time.time())
                               + float(head_min) * 60.0)
            changed = True
        if int(rec.get('total') or 0) != int(total):     # 客户改单量：以新值为准，进度保留
            rec['total'] = int(total)
            changed = True
        if per_day and int(rec.get('per_day') or 0) != int(per_day):
            rec['per_day'] = int(per_day)
            changed = True
        # 每次调度入口都推一次段：同一天内直接返回 False 不动账本，
        # 跨零点时才重开冲刺段，把新一天的额度先吃掉。
        if _roll(rec):
            changed = True
        if changed:
            _save(data)
        return rec
    now = time.time()
    rec = {
        'order_sn': order_sn,
        'total': int(total),
        'start_num': int(start_num or 0),
        'start_ts': now,
        'duration': float(hours) * 3600.0,
        'shape': shape if shape in SHAPES else DEFAULT_SHAPE,
        'head_pct': max(0.0, min(1.0, float(head_pct))),
        'head_min': float(head_min),
        'head_end': now + float(head_min) * 60.0,
        'delivered': 0,
        'day_log': {},
        'hour_log': {},
        'per_day': int(per_day or 0),
        'hourly_tol': float(DEFAULT_HOURLY_TOL),
        'lag': 0,
        'ticks': 0,
        'created': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    _roll(rec)                    # 建立第一段：seg_start / seg_base / seg_total / seg_dur
    data[order_sn] = rec
    _save(data)
    return rec


def _end_head_raw(order_sn: str, reason: str = '', retry: float = 0.0) -> dict:
    """提前结束冲刺段（桶关窗、素材耗尽、booster 判死）；给 retry 就安排一次重开。

    为什么要显式结束：冲刺段没有退避也没有闸，桶一旦关掉再打就是纯烧设备。
    转进尾段后小时闸立刻生效，节奏自然降到回补速率。

    为什么还要能重开：零增量只说明「此刻窗口关着」，实测窗口 5~140 分钟就会
    重开一次；一次打死就永久转尾段，等于把每次重开的窗口都让给尾段的
    十几条/小时。retry>0 时按 head_pause 暂停 retry 秒再重开冲刺，
    一段内最多重开 head_retry_max 次，避免死桶上无限烧设备。
    """
    data = _load()
    rec = data.get(order_sn)
    if not rec:
        return {}
    now = time.time()
    tries = int(rec.get('head_retries') or 0)
    limit = int(rec.get('head_retry_max') or DEFAULT_HEAD_RETRIES)
    if retry > 0 and tries < limit and float(rec.get('head_end') or 0) > now:
        pause = min(3600.0, float(retry) * (2 ** tries))
        rec['head_retries'] = tries + 1
        rec['head_pause'] = now + pause
        # 暂停不消耗冲刺窗口：head_end 同步往后推 pause 秒。
        # 不推的话 12:30 一到 in_head 立刻永久为假，前面安排的六次重开全废，
        # 窗口 15:00 重开时只剩尾段的七八十条/小时在爬。
        rec['head_end'] = float(rec.get('head_end') or now) + pause
        rec['head_end_reason'] = f'{reason or "closed"}+retry{tries + 1}/{limit}'
        _save(data)
        return rec
    if float(rec.get('head_end') or 0) > now:
        rec['head_end'] = now
        rec['head_pause'] = 0.0
        rec['head_end_reason'] = reason or 'closed'
        _save(data)
    return rec


def _note_raw(order_sn: str, credited: int, dropped: int = 0) -> dict:
    """记一次真实入账（credited 必须是 share_count 实测增量，不是回执数）。"""
    data = _load()
    rec = data.get(order_sn)
    if not rec:
        return {}
    credited = max(0, int(credited))
    rec['delivered'] = int(rec.get('delivered') or 0) + credited
    rec['ticks'] = int(rec.get('ticks') or 0) + 1
    rec['lag'] = int(rec.get('lag') or 0) + max(0, int(dropped))
    day = _today()
    log = rec.setdefault('day_log', {})
    log[day] = int(log.get(day) or 0) + credited
    hlog = rec.setdefault('hour_log', {})
    hk = _hour_key()
    hlog[hk] = int(hlog.get(hk) or 0) + credited
    # 只留最近 72 个小时桶：小时桶只用于限速，不是对账依据，
    # 留太多会让 pace_state.json 随长时间运行无限膨胀。
    if len(hlog) > 72:
        for k in sorted(hlog)[:len(hlog) - 72]:
            hlog.pop(k, None)
    rec['last_ts'] = time.time()
    # 跨零点时在这里就把新段推出来：note 是唯一会推进 delivered 的地方，
    # 让它顺手负责分段，调度器下一轮拿到的 rec 就已经是「今天」的目标了。
    _roll(rec)
    _save(data)
    return rec


def _align_raw(order_sn: str, done_real: int) -> dict:
    """把账本对齐到服务端实测净增；只增不减，重复调用结果不变。

    为什么不加差值：12 个窗口会同时读到同一份旧账本，各自算一次「实测净增 - 已记」
    再累加，427 条被记了 5 次（实测 delivered 冲到 2135，单子立刻被判完成）。
    这里直接取 max(旧值, 实测值)，谁写几遍都一样。
    """
    data = _load()
    rec = data.get(order_sn)
    if not rec:
        return {}
    done_real = max(0, int(done_real))
    old = int(rec.get('delivered') or 0)
    if done_real > old:
        add = done_real - old
        rec['delivered'] = done_real
        log = rec.setdefault('day_log', {})
        d = _today()
        log[d] = int(log.get(d) or 0) + add
        hlog = rec.setdefault('hour_log', {})
        hk = _hour_key()
        hlog[hk] = int(hlog.get(hk) or 0) + add
        rec['last_ts'] = time.time()
        _roll(rec)
        _save(data)
    return rec


def align(order_sn: str, done_real: int) -> dict:
    with _lock():
        return _align_raw(order_sn, done_real)


def _retune_raw(order_sn: str, hours: float = DEFAULT_HOURS,
                head_pct: float = DEFAULT_HEAD_PCT,
                head_min: float = DEFAULT_HEAD_MIN) -> dict:
    """把已存在记录的节奏参数改到新口径，一个入账数字都不动。

    为什么需要它：ensure 对已存在的记录只补缺失字段、不覆盖 duration / head_pct /
    head_min——这是为了防止进程重启把曲线重置回起点。可调度端的节奏参数一改，
    还在跑的旧单就卡在建单那天的口径上：12 小时摊平、35% 冲刺、冲刺窗口早过期，
    一整天只剩尾段的几十条/小时，客户看到的就是「刷不动」。
    这里只改参数与段边界，delivered / day_log / hour_log 一律不碰，
    而且结果对同一组参数是幂等的，重复调用不会反复改写文件。
    """
    data = _load()
    rec = data.get(order_sn)
    if not rec:
        return {}
    now = time.time()
    changed = False
    dur = float(hours) * 3600.0
    pct = max(0.0, min(1.0, float(head_pct)))
    hmin = float(head_min)
    if float(rec.get('duration') or 0) != dur:
        rec['duration'] = dur
        changed = True
    if abs(float(rec.get('head_pct') or 0.0) - pct) > 1e-9:
        rec['head_pct'] = pct
        changed = True
    if float(rec.get('head_min') or 0) != hmin:
        rec['head_min'] = hmin
        changed = True

    total = int(rec.get('total') or 0)
    seg_start = float(rec.get('seg_start') or rec.get('start_ts') or now)
    seg_end = seg_start + float(rec.get('seg_dur') or dur)
    if seg_end <= now:
        # 段已经过期（旧参数下被 deadline 截到只剩几小时）：就地重开一整段，
        # 段起点挪到当下、seg_base 对齐当前入账，冲刺目标仍然等于整单剩余量。
        rec['seg_start'] = now
        rec['seg_base'] = int(rec.get('delivered') or 0)
        rec['seg_dur'] = dur
        if not int(rec.get('per_day') or 0):
            rec['seg_total'] = total
        seg_start = now
        seg_end = now + dur
        changed = True
    elif float(rec.get('seg_dur') or 0) < dur:
        # 段还没过期，但比新口径短（旧账本 12 小时一段）：就地拉长，不换段起点。
        rec['seg_dur'] = dur
        seg_end = seg_start + dur
        changed = True

    want_head = min(seg_end, now + hmin * 60.0)
    if want_head > float(rec.get('head_end') or 0):
        rec['head_end'] = want_head
        rec['head_end_reason'] = 'retune'
        rec['head_pause'] = 0.0
        rec['head_retries'] = 0
        changed = True

    if changed:
        _save(data)
    return rec


def retune(order_sn: str, hours: float = DEFAULT_HOURS,
           head_pct: float = DEFAULT_HEAD_PCT,
           head_min: float = DEFAULT_HEAD_MIN) -> dict:
    with _lock():
        return _retune_raw(order_sn, hours=hours, head_pct=head_pct, head_min=head_min)


def ensure(order_sn: str, total: int, start_num: int = 0,
           hours: float = DEFAULT_HOURS, shape: str = DEFAULT_SHAPE,
           per_day: int = DEFAULT_PER_DAY, head_pct: float = DEFAULT_HEAD_PCT,
           head_min: float = DEFAULT_HEAD_MIN, restart: bool = False) -> dict:
    with _lock():
        return _ensure_raw(order_sn, total, start_num=start_num, hours=hours,
                           shape=shape, per_day=per_day, head_pct=head_pct,
                           head_min=head_min, restart=restart)


def note(order_sn: str, credited: int, dropped: int = 0) -> dict:
    with _lock():
        return _note_raw(order_sn, credited, dropped=dropped)


def end_head(order_sn: str, reason: str = '', retry: float = 0.0) -> dict:
    with _lock():
        return _end_head_raw(order_sn, reason=reason, retry=retry)


def get(order_sn: str) -> dict:
    return _load().get(order_sn) or {}


def done(order_sn: str) -> bool:
    rec = get(order_sn)
    return bool(rec) and int(rec.get('delivered') or 0) >= int(rec.get('total') or 0)


# ---------------------------------------------------------------- 命令行
def _fmt(rec: dict, now: float = None) -> str:
    now = now or time.time()
    total = int(rec.get('total') or 0)
    got = int(rec.get('delivered') or 0)
    phase = '冲刺' if in_head(rec, now) else '尾段'
    elapsed = now - float(rec.get('start_ts') or now)
    duration = float(rec.get('duration') or 1)
    eta = next_check(rec, now)
    return (f"{rec.get('order_sn')} | {phase} | {rec.get('shape')} | 总 {total} | "
            f"已入账 {got} | 应达 {due(rec, now)} | 冲刺目标 {head_target(rec)} | "
            f"进度 {got / total * 100 if total else 0:.1f}% | "
            f"已跑 {elapsed / 3600:.2f}h / {duration / 3600:.1f}h | "
            f"lag {int(rec.get('lag') or 0)} | 下一条 {time.strftime('%H:%M:%S', time.localtime(eta))}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=('list', 'show', 'sim', 'reset'))
    ap.add_argument('order_sn', nargs='?')
    ap.add_argument('--hours', type=float, default=DEFAULT_HOURS)
    ap.add_argument('--total', type=int, default=1800)
    ap.add_argument('--shape', default=DEFAULT_SHAPE)
    ap.add_argument('--head-pct', type=float, default=DEFAULT_HEAD_PCT)
    ap.add_argument('--head-min', type=float, default=DEFAULT_HEAD_MIN)
    a = ap.parse_args(argv)

    if a.cmd == 'list':
        data = _load()
        if not data:
            print('账本是空的')
            return 0
        for rec in data.values():
            print(_fmt(rec))
        return 0
    if not a.order_sn:
        print('需要 order_sn')
        return 2
    if a.cmd == 'reset':
        data = _load()
        data.pop(a.order_sn, None)
        _save(data)
        print(f'已删除 {a.order_sn}')
        return 0
    if a.cmd == 'show':
        rec = get(a.order_sn)
        if not rec:
            print(f'{a.order_sn} 不在账本里')
            return 1
        print(json.dumps(rec, ensure_ascii=False, indent=1))
        print(_fmt(rec))
        return 0
    if a.cmd == 'sim':
        rec = ensure(a.order_sn, a.total, hours=a.hours, shape=a.shape,
                     head_pct=a.head_pct, head_min=a.head_min, restart=True)
        now = time.time()
        print(f"{'分钟':>5s} {'应达':>6s} {'单批上限':>8s}")
        marks = [0, 5, 10, 20, 30, 45, 60, 120, 180, 360, 540, 720]
        for m in marks:
            if m > a.hours * 60:
                continue
            t = now + m * 60
            print(f'{m:>5d} {due(rec, t):>6d} {allowance(rec, t, chunk=40):>8d}')
        data = _load()
        data.pop(a.order_sn, None)       # sim 不留在账本里
        _save(data)
        return 0
    return 0


if __name__ == '__main__':
    sys.exit(main())
