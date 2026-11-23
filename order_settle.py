# -*- coding: utf-8 -*-
"""order_settle.py —— 订单交付切片 + 到期结算退款台账（分享端）

交付口径（与业务确认过的版本，不要再改回去）：
    1. total <= 1200：单日单。只在接单当日交付，当天收工就结算，
       缺多少退多少，绝不把余量挂到次日。
    2. total >  1200：多日单。days = ceil(total / 1200) 在接单时算定，
       前 days-1 天各吃满一个切片（默认 1200），只有第 days 天（末天）收工才结算，
       退款 = total - 累计官方净增。
    3. 例外：视频中途不可读（下架/删除/私密/统计连续读不到）→ 任何一天立即结算。

为什么必须单独成文件：
    · 现有三份台账都不记「这单还剩多少、哪天到期、退过没有」：
      pace_state.json 记节奏，baseline/ 记起点，used_devices.db 记设备与视频日账。
    · 结算必须能在「主循环已经不碰这条单」的时候发生：单被打进 day_skip 推到次日
      零点、或窗口重启，主循环都不会再进打量分支，到期扫描必须独立成一条路径。
    · 退款是真金白银，必须幂等：报过一次就落盘，重启、并发、重复扫描都不重报。

一份订单一个文件 settle/s_<order_sn>.json，写入走 tmp + os.replace（原子替换），
结算互斥用 settle/.lock_<order_sn>（O_EXCL + 持锁进程判活 + TTL 兜底）。
"""
import json
import math
import os
import time
from datetime import date, datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(BASE, 'settle')

# 切片基准：业务口径里「每个视频每天最多刷 1200」的 1200。
# 注意与货号日限（cfg 里的 video_day_cap，997 是 0 = 不限）区分：
# 日限是平台侧真实天花板，切片是我们的交付节奏。997 没有平台日限时，
# 仍然要按 1200 切片交给两天，否则 2300 条会被一天吃完，末天没有剩余可结算。
SLICE = int(os.environ.get('SHARE_SLICE_CAP', 1200) or 1200)
if SLICE <= 0:
    SLICE = 1200

# 结算窗口：末天（单日单就是当天）过了这个本地时刻才允许结算。
# 为什么不一停下就退：白天关窗/限流往往几十分钟后又开，太早结算等于把能刷的量退掉；
# 22:30 之后到零点的量本来也进不了账（回填还要 60 秒），这时候结算最准。
SETTLE_AFTER = os.environ.get('SHARE_SETTLE_AFTER', '22:30')
# 超期宽限：deadline 当天没结算成功（窗口全挂/接口故障），次日零点后仍会补结算。
GRACE_HOURS = float(os.environ.get('SHARE_SETTLE_GRACE_HOURS', 6))
# 统计连续读不到多少次就判「视频不可读」，走立即结算。
FAIL_DEAD = int(os.environ.get('SHARE_SETTLE_FAIL_DEAD', 6))
LOCK_TTL = float(os.environ.get('SHARE_SETTLE_LOCK_TTL', 900))


def _hm(text):
    try:
        hh, mm = str(text).split(':')[:2]
        return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
    except Exception:
        return 22, 30


_HH, _MM = _hm(SETTLE_AFTER)


def _path(order_sn):
    return os.path.join(DIR, 's_{}.json'.format(str(order_sn)))


def _lock_path(order_sn):
    return os.path.join(DIR, '.lock_{}.lck'.format(str(order_sn)))


def now_ts(now=None):
    return time.time() if now is None else float(now)


def today_str(now=None):
    return datetime.fromtimestamp(now_ts(now)).strftime('%Y-%m-%d')


def _d(day):
    try:
        return datetime.strptime(str(day), '%Y-%m-%d').date()
    except Exception:
        return date.today()


def day_add(day, n):
    return (_d(day) + timedelta(days=int(n))).strftime('%Y-%m-%d')


def slice_cap(day_cap=0):
    """本单每天最多交付多少条。

    day_cap 是货号的平台日限（1594=1200，997=0 不限）。取两者较大的那个没有意义，
    取较小的那个会把 997 压成 0（不限）。正确口径：有日限就用日限，没日限用切片。
    """
    cap = int(day_cap or 0)
    return cap if cap > 0 else SLICE


def days_of(total, cap=None):
    total = max(0, int(total or 0))
    cap = int(cap or SLICE) or SLICE
    if total <= 0:
        return 1
    return max(1, int(math.ceil(float(total) / float(cap))))


def plan(order_sn, aweme_id, total, goods='', day_cap=0, start_day=None, base=0, now=None,
         dry=False):
    """接单时建账，返回记录。已存在就直接返回（先写者胜）。

    为什么建账要落盘：多日单要跨零点、跨窗口、跨重启，days / 切片 / 起点 / 已报退款
    这四样只要有一个丢了，结算就会算错数或重报退款。
    dry=True 只返回内存记录，不落盘：演练用的是真实订单号，写进生产台账会把
    到期扫描引到一条根本没在刷的单上。
    """
    order_sn = str(order_sn)
    exist = load(order_sn)
    if exist:
        return exist
    total = max(0, int(total or 0))
    cap = slice_cap(day_cap)
    days = days_of(total, cap)
    sd = start_day or today_str(now)
    rec = {
        'order_sn': order_sn,
        'aweme_id': str(aweme_id or ''),
        'goods': str(goods or ''),
        'total': total,
        'cap': cap,
        'day_cap': int(day_cap or 0),
        'days': days,
        'start_day': sd,
        'deadline': day_add(sd, days - 1),
        'base': int(base or 0),
        'credited': 0,
        'day_log': [],
        'status': 'in_progress',      # in_progress / paid / pending_confirm / refunded
        'refund_n': 0,
        'refund_ts': 0,
        'refund_reported': False,
        'refund_resp': '',
        'state_written': '',
        'settle_reason': '',
        'cred_fail': 0,
        'created': int(now_ts(now)),
        'updated': int(now_ts(now)),
    }
    if dry:
        return rec
    try:
        os.makedirs(DIR, exist_ok=True)
        fd = os.open(_path(order_sn), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return load(order_sn) or rec
    except Exception:
        return rec
    try:
        os.write(fd, json.dumps(rec, ensure_ascii=False).encode('utf-8'))
    finally:
        os.close(fd)
    return load(order_sn) or rec


def set_base(rec, base):
    """接管视频时基线重设：base 换成此刻的绝对分享量，已交付归零重算。

    为什么不清 day_log：接管意味着本单还没在这个视频上交付过（量是上一单的），
    历史日志里那几条 credited 全是别人的，留着会让「累计净增」被重复计入。
    """
    rec['base'] = int(base or 0)
    rec['credited'] = 0
    rec['day_log'] = []
    save(rec)
    return rec


def load(order_sn):
    try:
        with open(_path(order_sn), encoding='utf-8') as fh:
            rec = json.load(fh) or {}
        if rec.get('order_sn'):
            return rec
    except Exception:
        pass
    return None


def save(rec):
    if not rec or not rec.get('order_sn'):
        return False
    rec['updated'] = int(time.time())
    try:
        os.makedirs(DIR, exist_ok=True)
        tmp = _path(rec['order_sn']) + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(rec, fh, ensure_ascii=False)
        os.replace(tmp, _path(rec['order_sn']))
        return True
    except Exception:
        return False


def day_index(rec, today=None):
    """当前是交付周期的第几天（1 起）。超过 days 说明已经超期，返回真实索引。"""
    start = _d(rec.get('start_day'))
    cur = _d(today or today_str())
    delta = (cur - start).days
    if delta < 0:
        return 1
    return delta + 1


def seg_of(rec, idx):
    """第 idx 天的交付配额。末天（含单日单）拿的是余量，不是整切片。"""
    total = int(rec.get('total') or 0)
    cap = int(rec.get('cap') or SLICE)
    days = int(rec.get('days') or 1)
    if idx >= days:
        return max(0, total - cap * (days - 1))
    return max(0, min(cap, total))


def quota_today(rec, credited, today=None):
    """今天还能交付多少条。0 表示今天不该再刷（切片已吃满 / 已超期 / 已交付完）。

    为什么不能只写 min(余量, 整片)：切片是「每天最多吃这么多」，
    必须扣掉今天已经吃进去的部分。少扣这一下，2300 的单第一天吃到 1200 之后
    quota 还会回 1100，切片等于不存在，第一天就把第二天的份额吃掉了。
    """
    rem = int(rec.get('total') or 0) - int(credited or 0)
    if rem <= 0:
        return 0
    idx = day_index(rec, today)
    days = int(rec.get('days') or 1)
    if idx > days:
        return 0                       # 超期：只能结算，不能再刷
    cap = int(rec.get('cap') or SLICE)
    prev = cap * (idx - 1)                          # 前 idx-1 片的额定总量
    day_done = max(0, int(credited or 0) - prev)    # 今天这一片已经吃进去多少
    return max(0, min(rem, seg_of(rec, idx) - day_done))


def deadline_ts(rec):
    """末天结算闸的时间戳：deadline 当天 SETTLE_AFTER 之后才允许结算。"""
    d = _d(rec.get('deadline'))
    return time.mktime((d.year, d.month, d.day, _HH, _MM, 0, 0, 0, -1))


def overdue_ts(rec):
    """硬截止：deadline 之后再过 GRACE_HOURS，无论有没有到结算时刻都必须结。"""
    d = _d(rec.get('deadline')) + timedelta(days=1)
    return time.mktime((d.year, d.month, d.day, 0, 0, 0, 0, 0, -1)) + GRACE_HOURS * 3600.0


def is_final_day(rec, today=None):
    return _d(today or today_str()) >= _d(rec.get('deadline'))


def decide(rec, credited, today=None, now=None, video_dead=False, video_gone=False):
    """结算决策。返回 {action, refund_n, reason, day_index, quota}。

    action：
        idle    已结算过，什么都不用做
        done    交付够了，写完成(4)
        carry   还没到结算点，继续刷（中间天 / 末天未到结算时刻）
        refund  该退款了，refund_n 条
    """
    ts = now_ts(now)
    td = today or today_str(ts)
    idx = day_index(rec, td)
    credited = max(0, int(credited or 0))
    quota = quota_today(rec, credited, td)
    out = {'action': 'carry', 'refund_n': 0, 'reason': '', 'day_index': idx,
           'quota': quota, 'credited': credited}
    status = str(rec.get('status') or 'in_progress')
    if status != 'in_progress':
        out['action'] = 'idle'
        out['reason'] = 'status={}'.format(status)
        return out
    rem = int(rec.get('total') or 0) - credited
    if rem <= 0:
        out['action'] = 'done'
        out['reason'] = '已交付 {}/{}'.format(credited, rec.get('total'))
        return out
    # 视频被用户删掉 / 自己设成私密 / 被平台封禁：标的不存在了，不是我们没交付。
    # 业务口径：这几种情况一律判完成，一分不退。
    # 放在 rem<=0 之后：已经刷够的单走正常 done 路径，备注里不会多一句「视频已删除」。
    # 必须先于 cred_dead 判断：删掉的视频也会「读不到」，走 cred_dead 就成了退款。
    if video_gone:
        out['action'] = 'done'
        out['reason'] = '视频已删除（{}），按完成处理'.format(
            rec.get('video_gone') or 'gone')
        return out
    # 视频不可读不必等末天：已经交付多少算多少，剩下的当场退。
    # 判据在台账里（cred_fail 连续读不到 FAIL_DEAD 次），调用方漏传 video_dead 时
    # 也能从台账自己认出来，不靠调用方自觉。
    if video_dead or cred_dead(rec):
        out['action'] = 'refund'
        out['refund_n'] = rem
        out['reason'] = '视频不可读'
        return out
    if _d(td) > _d(rec.get('deadline')):
        out['action'] = 'refund'
        out['refund_n'] = rem
        out['reason'] = '已超期（末天 {}）'.format(rec.get('deadline'))
        return out
    if _d(td) < _d(rec.get('deadline')):
        out['reason'] = '第 {}/{} 天，非末天不结算'.format(idx, rec.get('days'))
        return out
    # 末天：等结算窗口
    if ts >= deadline_ts(rec):
        out['action'] = 'refund'
        out['refund_n'] = rem
        out['reason'] = '第 {} 天收工，缺 {} 条'.format(idx, rem)
        return out
    out['reason'] = '末天 {}，{} 后才结算'.format(rec.get('deadline'), SETTLE_AFTER)
    return out


def note(rec, credited, today=None, now=None):
    """把这一轮的官方净增记进台账（同日覆盖、跨日追加）。"""
    credited = max(0, int(credited or 0))
    td = today or today_str(now)
    rec['credited'] = credited
    log = rec.get('day_log') or []
    for row in log:
        if str(row.get('day')) == td:
            row['credited'] = credited
            row['ts'] = int(now_ts(now))
            break
    else:
        log.append({'day': td, 'credited': credited, 'ts': int(now_ts(now))})
    rec['day_log'] = log[-30:]
    rec['cred_fail'] = 0
    # 能读到计数就说明视频还在：把「已删除」标记摘掉，否则一次误判会把这条单
    # 永久钉在完成态，后面真刷够了也走不回正常结算。
    rec.pop('video_gone', None)
    save(rec)
    return rec


def note_fail(rec):
    """统计读不到一次。连续读不到 FAIL_DEAD 次判视频不可读。"""
    rec['cred_fail'] = int(rec.get('cred_fail') or 0) + 1
    save(rec)
    return rec['cred_fail']


def cred_dead(rec):
    return int(rec.get('cred_fail') or 0) >= FAIL_DEAD


def settle(rec, credited, action, refund_fn=None, state_fn=None, dry=True, now=None):
    """执行结算：写完成(4) 或 报退款 + 写退款(7)。幂等，重复调用不会重复报。

    refund_fn(order_sn, number, remark) -> 平台返回文本
    state_fn(order_sn, state, remark)   -> 平台返回文本
    dry=True 只落本地账、不碰平台接口，用于演练与自测。
    """
    sn = str(rec.get('order_sn'))
    ts = now_ts(now)
    credited = max(0, int(credited or 0))
    out = {'order_sn': sn, 'ok': True, 'action': action, 'refund_n': 0,
           'reported': False, 'state': '', 'msg': ''}
    status = str(rec.get('status') or 'in_progress')
    if status in ('paid', 'pending_confirm', 'refunded') and not dry:
        out['action'] = 'idle'
        out['msg'] = '已结算过：status={}'.format(status)
        return out
    rem = int(rec.get('total') or 0) - credited
    rec['credited'] = credited

    if action == 'done' or rem <= 0:
        # 备注要能看出为什么算完成：视频被删掉的单一条都没刷完就写了 4，
        # 平台上如果只留一句「订单完成」，客诉时没人能解释这批单是怎么结的。
        gone = str(rec.get('video_gone') or '')
        if gone:
            reason = '视频已删除({})，按完成处理，已交付{}/{}'.format(
                gone, credited, rec.get('total'))
        else:
            reason = '已交付 {}/{}'.format(credited, rec.get('total'))
        rec['settle_reason'] = reason
        out['action'] = 'done'
        if dry:
            out['msg'] = 'dry：写 4（{}）'.format(reason)
            return out
        if state_fn:
            try:
                out['state'] = str(state_fn(sn, 4, reason))
                rec['state_written'] = '4'
            except Exception as exc:
                # 状态没写进去就不许把本地记成 paid：记了之后 decide 一律返回 idle，
                # 平台那条单永远停在 2，谁都看不见它，钱和单一起失踪。
                out['ok'] = False
                out['msg'] = '状态4写入失败：{}'.format(str(exc)[:120])
                save(rec)
                return out
        rec['status'] = 'paid'
        save(rec)
        return out

    if action != 'refund':
        out['action'] = action
        out['msg'] = '非结算动作，未落账'
        return out

    # 天数必须按本次结算的时刻算，不能用系统当前时间：
    # 补结算（次日才发现）如果用当前时间算，多日单会被写回「第1天」，
    # 台账和平台备注就对不上账了。
    remark = '第{}天收工，缺{}条退款'.format(day_index(rec, today_str(ts)), rem)
    rec['refund_n'] = rem
    out['refund_n'] = rem
    out['action'] = 'refund'
    if dry:
        out['msg'] = 'dry：报退款 {} 条（{}）'.format(rem, remark)
        rec['settle_reason'] = 'dry ' + remark
        return out

    if not rec.get('refund_reported'):
        if not refund_fn:
            out['ok'] = False
            out['msg'] = '没有退款函数，未上报'
            return out
        try:
            resp = refund_fn(sn, rem, remark)
        except Exception as exc:
            # 接口失败就当没报过：不写 refund_reported，下一轮扫描重试。
            # 写在这里而不是让异常冒出去，是因为扫描是一批一批走的，
            # 一条单的网络故障不该把整批剩下那些单的结算一起拖死。
            out['ok'] = False
            out['msg'] = '退款上报失败：{}'.format(str(exc)[:160])
            save(rec)
            return out
        rec['refund_reported'] = True
        rec['refund_ts'] = int(ts)
        rec['refund_resp'] = str(resp)[:500]
        rec['status'] = 'pending_confirm'
        rec['settle_reason'] = remark
        out['reported'] = True
        out['msg'] = '报退款 {} 条 | {}'.format(rem, str(resp)[:120])
    else:
        out['msg'] = '退款已报过（{} 条，ts={}），不重报'.format(
            rec.get('refund_n'), rec.get('refund_ts'))
    if state_fn:
        try:
            out['state'] = str(state_fn(sn, 7, remark))
            rec['state_written'] = '7'
        except Exception as exc:
            out['ok'] = False
            out['msg'] = '{} | 状态7写入失败：{}'.format(out['msg'], str(exc)[:120])
    save(rec)
    return out


def confirm(rec, platform_state):
    """按平台订单状态推进本地记录：4 → paid，7 → refunded。"""
    try:
        st = int(platform_state or 0)
    except Exception:
        return rec
    if st == 4 and rec.get('status') == 'in_progress':
        rec['status'] = 'paid'
        save(rec)
    elif st == 7:
        rec['status'] = 'refunded'
        save(rec)
    return rec


def due(now=None, batch=20):
    """到期需要结算的订单：末天已过结算时刻，或已超期，或视频被标记不可读。"""
    ts = now_ts(now)
    td = today_str(ts)
    out = []
    try:
        names = os.listdir(DIR)
    except Exception:
        return out
    for name in names:
        if not (name.startswith('s_') and name.endswith('.json')):
            continue
        rec = None
        try:
            with open(os.path.join(DIR, name), encoding='utf-8') as fh:
                rec = json.load(fh) or {}
        except Exception:
            continue
        if not rec or str(rec.get('status')) != 'in_progress':
            continue
        if ts < overdue_ts(rec):
            if _d(td) < _d(rec.get('deadline')):
                continue
            if ts < deadline_ts(rec) and not cred_dead(rec):
                continue
        out.append(rec)
    out.sort(key=lambda r: (r.get('deadline') or '', r.get('order_sn') or ''))
    return out[:max(1, int(batch))]


def _pid_alive(pid):
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
        except Exception:
            return False
    try:
        import ctypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.WaitForSingleObject.restype = ctypes.c_uint32
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(0x00100000, 0, pid)
        if not h:
            return False
        try:
            return k32.WaitForSingleObject(h, 0) == 0x102
        finally:
            k32.CloseHandle(h)
    except Exception:
        return True


def lock(order_sn):
    """结算互斥：同一单同一时刻只有一个窗口在报退款。"""
    try:
        os.makedirs(DIR, exist_ok=True)
    except Exception:
        return False
    path = _lock_path(order_sn)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, ('%d %.0f\n' % (os.getpid(), time.time())).encode())
            os.close(fd)
            return True
        except FileExistsError:
            holder, ts = 0, 0.0
            try:
                with open(path, encoding='utf-8') as fh:
                    parts = fh.read().split()
                holder, ts = int(parts[0]), float(parts[1])
            except Exception:
                pass
            if not _pid_alive(holder) or (time.time() - ts) > LOCK_TTL:
                try:
                    os.remove(path)
                except Exception:
                    pass
                continue
            return False
        except Exception:
            return False
    return False


def unlock(order_sn):
    try:
        os.remove(_lock_path(order_sn))
        return True
    except Exception:
        return False


def stats():
    """台账概览：按状态计数 + 已报退款条数合计。"""
    out = {'in_progress': 0, 'paid': 0, 'pending_confirm': 0, 'refunded': 0,
           'refund_total': 0, 'files': 0}
    try:
        names = os.listdir(DIR)
    except Exception:
        return out
    for name in names:
        if not (name.startswith('s_') and name.endswith('.json')):
            continue
        try:
            with open(os.path.join(DIR, name), encoding='utf-8') as fh:
                rec = json.load(fh) or {}
        except Exception:
            continue
        out['files'] += 1
        st = str(rec.get('status') or '')
        if st in out:
            out[st] += 1
        if rec.get('refund_reported'):
            out['refund_total'] += int(rec.get('refund_n') or 0)
    return out
