# -*- coding: utf-8 -*-
"""设备-视频台账（按自然日重置的持久化去重）

三条时间规则全部定义在这个文件里，别在别处再写一遍：

    1 去重键 (device_id, aweme_id)：同一台设备对同一条视频，每个自然日只贡献一次
    2 自然日零点重置：昨天用过的设备，今天对同一条视频重新可用
       —— 客户第二天续刷同一条视频，直接接着刷，不需要人工清库
    3 单条视频每天最多入账 DAY_CAP=1200 条：跨进程共享，靠 video_day 表计数

为什么按自然日而不是滑动 24 小时：客户口径「第二天能接着刷同一条视频」。
滑动窗口会把昨晚 23 点跑过的设备压到今天 23 点才解封，等于白等一天。
需要严格 24 小时冷却时，把 PAIR_COOLDOWN_HOURS 设成 24 即可切回滑动窗口。

失败记录（ok=0）不再永久烧名额：FAIL_RETRY_SECONDS 之后允许重试同一对(设备,视频)。
为什么：一次 5 秒超时属于链路抖动，不该让一台好设备在这条视频上永久失效，
否则 1200 的量跑不满，还得人工清库。
"""
import os
import time
import sqlite3
import threading

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "used_devices.db")
_lock = threading.Lock()
# 每线程一条常驻连接：原来每次操作都新建连接并重跑 5 条 CREATE TABLE + 3 条 PRAGMA，
# 一次 burst 前置（8000 台设备逐台 tried()）就是 6 万多条建表语句、几十秒空转，
# 而且这些连接全程抢写锁。连接常驻 + 建表一次之后这部分开销归零。
_local = threading.local()

# ---- 可调参数：改这里，不用动下面任何逻辑
# 单条视频每个自然日最多入账多少条分享。
# 默认值从 1800 抬到 100000：1800 是「本地假天花板」，平台真实上限另有一道
# （实测 341~639/天，撞上去 delta 会自然归零），本地这道只会提前把单子推到明天。
# 交付口径是「客户下多少刷多少」，所以本地这道默认形同不存在；真需要限流时
# 用 SHARE_DAY_CAP 显式给数字，别给 0——day_cap_hit 是 used >= cap，0 会恒为真。
DAY_CAP = int(os.environ.get("SHARE_DAY_CAP", 100000))
PAIR_COOLDOWN_HOURS = 0     # 0 = 自然日零点重置；24 = 同一对(设备,视频)间隔 24 小时
FAIL_RETRY_SECONDS = 1800   # 失败记录多久后允许重新尝试同一对(设备,视频)


# 建表语句集中在这里，连接建立时只跑一遍
_DDL = (
    "CREATE TABLE IF NOT EXISTS used("
    " device_id TEXT NOT NULL,"
    " aweme_id  TEXT NOT NULL,"
    " ts        REAL NOT NULL,"
    " ok        INTEGER NOT NULL DEFAULT 0,"
    " PRIMARY KEY(device_id, aweme_id))",
    "CREATE INDEX IF NOT EXISTS idx_aweme ON used(aweme_id)",
    # 设备每日总分享次数：跨视频累计，用来卡「一台设备一天最多贡献几条视频」
    "CREATE TABLE IF NOT EXISTS device_day("
    " device_id TEXT NOT NULL,"
    " day       TEXT NOT NULL,"
    " cnt       INTEGER NOT NULL DEFAULT 0,"
    " last_ts   REAL NOT NULL DEFAULT 0,"
    " PRIMARY KEY(device_id, day))",
    "CREATE INDEX IF NOT EXISTS idx_day ON device_day(day)",
    # 视频每日入账量：跨设备、跨进程累计，用来卡「一条视频一天最多 DAY_CAP 条」
    "CREATE TABLE IF NOT EXISTS video_day("
    " aweme_id TEXT NOT NULL,"
    " day      TEXT NOT NULL,"
    " cnt      INTEGER NOT NULL DEFAULT 0,"
    " last_ts  REAL NOT NULL DEFAULT 0,"
    " PRIMARY KEY(aweme_id, day))",
    "CREATE INDEX IF NOT EXISTS idx_vday ON video_day(day)",
    # 全局死号表：一台设备确认死了就写这里，所有接单进程共享，不用各自重试一遍。
    # 为什么落库而不是留在进程内存：4–12 个进程各持一份内存死号集合，同一台坏设备
    # 会被每个进程各试 BAD_LIMIT 次，等于白烧 2×进程数 条请求，还会把失败计数刷满。
    "CREATE TABLE IF NOT EXISTS dead("
    " device_id TEXT PRIMARY KEY,"
    " ts        REAL NOT NULL)",
)


def _conn() -> sqlite3.Connection:
    """取本线程的常驻连接，首次调用时建表。

    多进程高并发写：不开 WAL 时 12 个接单进程 × 24 线程会在写锁上互相撞死，
    抛出的 database is locked 会被上层当成「上报失败」，顺手把好设备标成死号。
    busy_timeout 让抢不到锁的进程等而不是立刻报错，synchronous=NORMAL 在 WAL 下安全且快。
    SQLite 连接不能跨线程共享，所以按 threading.local() 一线程一条。
    """
    c = getattr(_local, "c", None)
    if c is not None:
        return c
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("PRAGMA synchronous=NORMAL")
    for stmt in _DDL:
        c.execute(stmt)
    c.commit()
    _local.c = c
    return c


# ---------------------------------------------------------------- 时间口径
def today(ts=None) -> str:
    """自然日字符串，台账的日键。"""
    return time.strftime("%Y-%m-%d", time.localtime(time.time() if ts is None else ts))


def day_start(ts=None) -> float:
    """某个时刻所属自然日的零点时间戳。为什么用它：零点重置的边界必须只有一个。"""
    lt = time.localtime(time.time() if ts is None else ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def next_day_start(ts=None) -> float:
    """下一个自然日零点的时间戳。

    为什么单独写一个：今天额度用满的单子要排到「明天零点」，调用方如果自己
    算 +86400，遇到夏令时或手改系统时间就会飘，边界只留在这里一处。
    """
    return day_start(ts) + 86400.0


def pair_cutoff(now=None, window_hours=None) -> float:
    """成功记录的封锁起点：早于这个时间戳的 (设备,视频) 记录不再挡路。"""
    now = time.time() if now is None else now
    hours = PAIR_COOLDOWN_HOURS if window_hours is None else window_hours
    if hours and hours > 0:
        return now - hours * 3600.0
    return day_start(now)


def fail_cutoff(now=None, window_hours=None) -> float:
    """失败记录的封锁起点：至少等 FAIL_RETRY_SECONDS 再重试同一对，避免连点。"""
    now = time.time() if now is None else now
    return max(pair_cutoff(now, window_hours), now - FAIL_RETRY_SECONDS)


# ---------------------------------------------------------------- 设备日计数
def bump_day(device_id: str, day: str = ""):
    """该设备今天又贡献了一次分享（跨视频累计）"""
    day = day or today()
    with _lock:
        with _conn() as c:
            row = c.execute("SELECT cnt FROM device_day WHERE device_id=? AND day=?",
                            (device_id, day)).fetchone()
            if row:
                c.execute("UPDATE device_day SET cnt=cnt+1, last_ts=? "
                          "WHERE device_id=? AND day=?", (time.time(), device_id, day))
            else:
                c.execute("INSERT INTO device_day(device_id, day, cnt, last_ts) "
                          "VALUES(?,?,1,?)", (device_id, day, time.time()))


def day_state(device_id: str, day: str = "") -> tuple:
    """返回 (今日已分享次数, 上次使用时间戳)"""
    day = day or today()
    with _lock:
        with _conn() as c:
            row = c.execute("SELECT cnt, last_ts FROM device_day WHERE device_id=? AND day=?",
                            (device_id, day)).fetchone()
            return (row[0], row[1]) if row else (0, 0.0)


def day_used(device_id: str, day: str = "") -> int:
    return day_state(device_id, day)[0]


def quota_ok(device_id: str, gap_seconds: float = 0.0, soft_max: int = 0,
             day: str = "") -> bool:
    """这台设备现在能不能再贡献一条视频。

    注意：soft_max 不是平台配额，是我们自己给设备设的行为上限（默认 0=不限）。
    真正的硬约束是「同一设备对同一视频每天只算一次」，跨视频没有次数限制。
    gap_seconds 控制同一台设备两次分享之间的最小间隔，让它看起来像真人在一天里零散地分享。
    """
    cnt, last = day_state(device_id, day)
    if soft_max and cnt >= soft_max:
        return False
    if gap_seconds and time.time() - last < gap_seconds:
        return False
    return True


def day_report(day: str = "") -> dict:
    day = day or today()
    with _lock:
        with _conn() as c:
            row = c.execute("SELECT COUNT(*), COALESCE(SUM(cnt),0) FROM device_day WHERE day=?",
                            (day,)).fetchone()
    return {"day": day, "devices_used": row[0], "total_shares": row[1]}


# ---------------------------------------------------------------- 视频日入账量
def video_day_used(aweme_id: str, day: str = "") -> int:
    """这条视频今天已经入账多少条分享（我们这边上报成功的，不含自然流量）"""
    day = day or today()
    with _lock:
        with _conn() as c:
            row = c.execute("SELECT cnt FROM video_day WHERE aweme_id=? AND day=?",
                            (aweme_id, day)).fetchone()
            return row[0] if row else 0


def bump_video(aweme_id: str, n: int = 1, day: str = "", cap: int = None) -> bool:
    """记一次成功入账；给了 cap 就顺手卡上限，返回 False 表示已到顶、这次没计入。

    为什么在写库这一层卡上限：多个订单进程可能同时刷同一条视频，
    只在上层比大小会有竞态，写库时判断才是原子的。
    """
    day = day or today()
    cap = DAY_CAP if cap is None else int(cap)
    with _lock:
        with _conn() as c:
            row = c.execute("SELECT cnt FROM video_day WHERE aweme_id=? AND day=?",
                            (aweme_id, day)).fetchone()
            cnt = row[0] if row else 0
            if cap and cnt >= cap:
                return False
            if row:
                c.execute("UPDATE video_day SET cnt=cnt+?, last_ts=? "
                          "WHERE aweme_id=? AND day=?", (n, time.time(), aweme_id, day))
            else:
                c.execute("INSERT INTO video_day(aweme_id, day, cnt, last_ts) "
                          "VALUES(?,?,?,?)", (aweme_id, day, n, time.time()))
            return True


def refund_video(aweme_id: str, n: int = 1, day: str = "") -> int:
    """把已经记进当日额度的条数退回去，返回实际退回多少。

    为什么需要：服务端会对「同一设备同一视频」永久去重，重复上报照样返回 200 但不计数。
    上报端只有等计数落账后才知道哪几条是白发的，那部分必须从当天的 1800 里扣回来，
    否则空转请求会吃掉额度，客户当天永远补不齐。
    """
    n = int(n)
    if n <= 0:
        return 0
    day = day or today()
    with _lock:
        with _conn() as c:
            row = c.execute("SELECT cnt FROM video_day WHERE aweme_id=? AND day=?",
                            (aweme_id, day)).fetchone()
            cur = row[0] if row else 0
            back = min(cur, n)
            if back <= 0:
                return 0
            left = cur - back
            if left > 0:
                c.execute("UPDATE video_day SET cnt=?, last_ts=? "
                          "WHERE aweme_id=? AND day=?", (left, time.time(), aweme_id, day))
            else:
                c.execute("DELETE FROM video_day WHERE aweme_id=? AND day=?",
                          (aweme_id, day))
            return back


def sync_video_day(aweme_id: str, credited: int, day: str = "") -> int:
    """把本轮实测净增累加进当日入账，返回累加后的当日总数。

    为什么用 credited 而不是 accepted：accepted 是「请求被服务端收下」，credited 是
    「计数真的涨了」。前者实测虚高 3.5 倍（787 对 221），room 按它算会让订单
    提前判定完成，或者反过来一直补量。credited 才是唯一真值。

    为什么是累加而不是覆盖：credited 是「本轮 final - 本轮 baseline」，本身就是增量。
    覆盖会把前面几轮的入账抹掉——2026-11-16 实测：同一条视频当天真实计入 403 条，
    最后一行覆盖成 6 条（最后一轮的增量），于是 day_cap=1800 以为还剩 1794 条额度，
    调度器继续拿设备去撞已经关掉的桶。多进程跑同一条视频时覆盖更危险，谁最后写谁说了算。
    """
    day = day or today()
    credited = max(0, int(credited))
    if credited <= 0:
        return video_day_used(aweme_id, day)
    with _lock:
        with _conn() as c:
            row = c.execute("SELECT cnt FROM video_day WHERE aweme_id=? AND day=?",
                            (aweme_id, day)).fetchone()
            cur = row[0] if row else 0
            total = cur + credited
            if row:
                c.execute("UPDATE video_day SET cnt=?, last_ts=? "
                          "WHERE aweme_id=? AND day=?", (total, time.time(), aweme_id, day))
            else:
                c.execute("INSERT INTO video_day(aweme_id, day, cnt, last_ts) "
                          "VALUES(?,?,?,?)", (aweme_id, day, total, time.time()))
            return total


def video_room(aweme_id: str, day_cap: int = None, day: str = "") -> int:
    """这条视频今天还能入账多少条。day_cap<=0 视为不限。"""
    cap = DAY_CAP if day_cap is None else int(day_cap)
    if cap <= 0:
        return 10 ** 9
    return max(0, cap - video_day_used(aweme_id, day))


def day_cap_hit(aweme_id: str, day_cap: int = None, day: str = "") -> bool:
    """这条视频今天的额度是不是真的用满了。

    为什么单独判一次：并发时会有一批请求「预留了额度但最后失败」，预留不等于入账，
    只有台账里的实际计数到顶才算封顶，否则会把没跑满的视频错误地推到明天。
    """
    cap = DAY_CAP if day_cap is None else int(day_cap)
    if cap <= 0:
        return False
    return video_day_used(aweme_id, day) >= cap


def video_day_report(day: str = "") -> list:
    day = day or today()
    with _lock:
        with _conn() as c:
            return c.execute("SELECT aweme_id, cnt FROM video_day WHERE day=? "
                             "ORDER BY cnt DESC", (day,)).fetchall()


def reset_video_day(aweme_id: str, day: str = ""):
    """人工清零某条视频当天的入账量，只有需要重跑当天配额时才用。"""
    day = day or today()
    with _lock:
        with _conn() as c:
            c.execute("DELETE FROM video_day WHERE aweme_id=? AND day=?", (aweme_id, day))
    clear_block_cache(aweme_id)


# ---------------------------------------------------------------- 设备-视频配对
def free_devices(device_ids, aweme_id: str, gap_seconds: float = 0.0,
                 soft_max: int = 0, day: str = "", window_hours: float = None) -> list:
    """过滤出「这条视频今天没用过」且「距离上次分享已过 gap_seconds」的设备 id

    封锁判定：ok=1 的记录挡到自然日零点；ok=0 的记录只挡 FAIL_RETRY_SECONDS。
    soft_max=0 表示不限制每台设备每天贡献多少条视频——真机一天分享几十条都正常，
    设备池不需要按视频数线性扩，只需要控制节奏。
    """
    ids = list(dict.fromkeys(device_ids))
    if not ids:
        return []
    day = day or today()
    ok_cut = pair_cutoff(window_hours=window_hours)
    fail_cut = fail_cutoff(window_hours=window_hours)
    blocked, daymap = set(), {}
    with _lock:
        with _conn() as c:
            step = 500
            for i in range(0, len(ids), step):
                chunk = ids[i:i + step]
                q = ("SELECT device_id FROM used WHERE aweme_id=? "
                     "AND ((ok=1 AND ts>=?) OR (ok=0 AND ts>=?)) "
                     "AND device_id IN (%s)" % ",".join("?" * len(chunk)))
                blocked.update(r[0] for r in c.execute(
                    q, [aweme_id, ok_cut, fail_cut] + chunk))
                q2 = ("SELECT device_id, cnt, last_ts FROM device_day "
                      "WHERE day=? AND device_id IN (%s)" % ",".join("?" * len(chunk)))
                for did, cnt, last in c.execute(q2, [day] + chunk):
                    daymap[did] = (cnt, last)
    now = time.time()
    out = []
    for d in ids:
        if d in blocked:
            continue
        cnt, last = daymap.get(d, (0, 0.0))
        if soft_max and cnt >= soft_max:
            continue
        if gap_seconds and now - last < gap_seconds:
            continue
        out.append(d)
    return out


def is_used(device_id: str, aweme_id: str) -> bool:
    """这台设备在当前封锁窗口内是否已经占用过这个视频"""
    cut = pair_cutoff()
    with _lock:
        with _conn() as c:
            row = c.execute(
                "SELECT ok FROM used WHERE device_id=? AND aweme_id=? AND ts>=?",
                (device_id, aweme_id, cut)).fetchone()
            return bool(row and row[0])


def tried(device_id: str, aweme_id: str) -> bool:
    """当前是否连试都不能试（成功记录到零点、失败记录到 FAIL_RETRY_SECONDS）"""
    ok_cut = pair_cutoff()
    fail_cut = fail_cutoff()
    with _lock:
        with _conn() as c:
            return c.execute(
                "SELECT 1 FROM used WHERE device_id=? AND aweme_id=? "
                "AND ((ok=1 AND ts>=?) OR (ok=0 AND ts>=?))",
                (device_id, aweme_id, ok_cut, fail_cut)).fetchone() is not None


# ---------------------------------------------------------------- 封锁集合（内存叠加层）
_BLOCK_CACHE = {}
_BLOCK_LOCK = threading.Lock()


def blocked_ids(aweme_id: str, ttl: float = 5.0) -> set:
    """今天在这条视频上已占用的设备 id 集合，进程内缓存 ttl 秒。

    为什么要有：candidate_devices / free_devices 原来每台设备查一次库，
    8000 台就是 8000 次连接（每次还带 8 条建表语句）。这里一次取全表、
    在内存里比，选素材从几十秒降到毫秒。
    """
    now = time.time()
    with _BLOCK_LOCK:
        hit = _BLOCK_CACHE.get(aweme_id)
        if hit and now - hit[0] < ttl:
            return hit[1]
    ok_cut = pair_cutoff()
    fail_cut = fail_cutoff()
    with _lock:
        with _conn() as c:
            rows = c.execute(
                "SELECT device_id FROM used WHERE aweme_id=? "
                "AND ((ok=1 AND ts>=?) OR (ok=0 AND ts>=?))",
                (aweme_id, ok_cut, fail_cut)).fetchall()
    s = {r[0] for r in rows}
    with _BLOCK_LOCK:
        _BLOCK_CACHE[aweme_id] = (now, s)
    return s


def note_blocked(aweme_id: str, device_id: str):
    """写库成功后把设备同步进内存封锁集合，避免缓存过期前被重复派发。"""
    with _BLOCK_LOCK:
        hit = _BLOCK_CACHE.get(aweme_id)
        if hit:
            hit[1].add(device_id)


def clear_block_cache(aweme_id: str = ''):
    """清掉封锁集合缓存；人工清零某条视频额度后调用。"""
    with _BLOCK_LOCK:
        if aweme_id:
            _BLOCK_CACHE.pop(aweme_id, None)
        else:
            _BLOCK_CACHE.clear()


def mark(device_id: str, aweme_id: str, ok: bool):
    """记一条 (设备,视频) 占用。

    成功记录不会被当天的失败覆盖：同一台设备今天已经给这条视频贡献过一次，
    之后哪怕这次请求超时，也不能把 ok=1 降成 ok=0——一旦降级，FAIL_RETRY_SECONDS
    之后同一对会被重新派发，而服务端按 (device_id, aweme_id) 去重，等于白跑一趟
    还白占一个并发位，1200 的额度也就跑不满。
    昨天的成功记录不受保护：今天这次失败照样写进去，允许 30 分钟后重试。
    """
    now = time.time()
    cut = pair_cutoff(now)
    with _lock:
        with _conn() as c:
            row = c.execute("SELECT ok, ts FROM used WHERE device_id=? AND aweme_id=?",
                            (device_id, aweme_id)).fetchone()
            if ok:
                c.execute("INSERT OR REPLACE INTO used VALUES(?,?,?,1)",
                          (device_id, aweme_id, now))
            elif row and row[0] and float(row[1]) >= cut:
                c.execute("UPDATE used SET ts=? WHERE device_id=? AND aweme_id=?",
                          (now, device_id, aweme_id))
            else:
                c.execute("INSERT OR REPLACE INTO used VALUES(?,?,?,0)",
                          (device_id, aweme_id, now))
    note_blocked(aweme_id, device_id)


def used_ok(aweme_id: str, day: str = "") -> int:
    """这条视频今天有多少台设备成功贡献过（跨天自动从零开始）"""
    if day:
        cut = day_start(time.mktime(time.strptime(day, "%Y-%m-%d")))
    else:
        cut = pair_cutoff()
    with _lock:
        with _conn() as c:
            return c.execute("SELECT COUNT(*) FROM used WHERE aweme_id=? AND ok=1 AND ts>=?",
                             (aweme_id, cut)).fetchone()[0]


def tried_any(aweme_id: str, day: str = "") -> int:
    cut = (day_start(time.mktime(time.strptime(day, "%Y-%m-%d")))
           if day else day_start())
    with _lock:
        with _conn() as c:
            return c.execute("SELECT COUNT(*) FROM used WHERE aweme_id=? AND ts>=?",
                             (aweme_id, cut)).fetchone()[0]


def fresh_count(device_ids, aweme_id: str) -> int:
    """给定设备 id 列表，算今天还有多少台没在这个视频上试过"""
    ids = list(dict.fromkeys(device_ids))
    if not ids:
        return 0
    ok_cut = pair_cutoff()
    fail_cut = fail_cutoff()
    done = set()
    with _lock:
        with _conn() as c:
            step = 500
            for i in range(0, len(ids), step):
                chunk = ids[i:i + step]
                q = ("SELECT device_id FROM used WHERE aweme_id=? "
                     "AND ((ok=1 AND ts>=?) OR (ok=0 AND ts>=?)) "
                     "AND device_id IN (%s)" % ",".join("?" * len(chunk)))
                done.update(r[0] for r in c.execute(
                    q, [aweme_id, ok_cut, fail_cut] + chunk))
    return len([d for d in ids if d not in done])


# ---------------------------------------------------------------- 全局死号
def mark_dead(device_id: str, ts: float = None):
    """把一台设备写进全局死号表；别的接单进程刷新后立刻也不再派发它。"""
    with _lock:
        with _conn() as c:
            c.execute("INSERT OR REPLACE INTO dead(device_id, ts) VALUES(?,?)",
                      (device_id, time.time() if ts is None else float(ts)))


def dead_ids(max_age: float = 86400) -> set:
    """取最近 max_age 秒内被判死的设备 id 集合。

    为什么要过期：设备可能因为当天频控假死，隔天就恢复了；永久拉黑等于把池子越用越小。
    max_age<=0 表示取全部。
    """
    with _lock:
        with _conn() as c:
            if max_age and max_age > 0:
                rows = c.execute("SELECT device_id FROM dead WHERE ts>=?",
                                 (time.time() - max_age,))
            else:
                rows = c.execute("SELECT device_id FROM dead")
            return {r[0] for r in rows}


def revive_dead(device_id: str = ""):
    """人工复活：给 device_id 就只救这一台，空串清空整张表。"""
    with _lock:
        with _conn() as c:
            if device_id:
                return c.execute("DELETE FROM dead WHERE device_id=?",
                                 (device_id,)).rowcount
            return c.execute("DELETE FROM dead").rowcount


def dead_count(max_age: float = 86400) -> int:
    return len(dead_ids(max_age))


def purge(days: float = 2.0):
    """清掉 N 天前的记录。

    去重窗口已经是自然日，旧记录留着也不挡路；这个函数只负责把库压小。
    """
    cut = time.time() - days * 86400
    with _lock:
        with _conn() as c:
            n = c.execute("DELETE FROM used WHERE ts<?", (cut,)).rowcount
            n += c.execute("DELETE FROM device_day WHERE last_ts<? AND last_ts>0",
                           (cut,)).rowcount
            n += c.execute("DELETE FROM video_day WHERE last_ts<? AND last_ts>0",
                           (cut,)).rowcount
            n += c.execute("DELETE FROM dead WHERE ts<?", (cut,)).rowcount
    return n


def stats() -> dict:
    with _lock:
        with _conn() as c:
            total = c.execute("SELECT COUNT(*) FROM used").fetchone()[0]
            ok = c.execute("SELECT COUNT(*) FROM used WHERE ok=1").fetchone()[0]
            videos = c.execute("SELECT COUNT(DISTINCT aweme_id) FROM used").fetchone()[0]
            today_rows = c.execute("SELECT COUNT(*) FROM used WHERE ts>=?",
                                   (day_start(),)).fetchone()[0]
            dead = c.execute("SELECT COUNT(*) FROM dead WHERE ts>=?",
                             (time.time() - 86400,)).fetchone()[0]
    return {"rows": total, "ok": ok, "videos": videos, "today": today_rows,
            "dead": dead, "day_cap": DAY_CAP, "db": DB_PATH}


if __name__ == "__main__":
    print(stats())
