# -*- coding: utf-8 -*-
"""分享单接单 / 回收 / 完成 三合一守护进程。

和 12 个刷量窗口的分工：
  本进程只推进订单状态（1→2、3→2、2→4），不刷量、不占 order_<sn>.lock、
  不碰 pace 账本、不写 exec_num。刷量窗口自己也会写 4，写重了没有副作用。

为什么不塞进刷量窗口：
  1) get_order_list 用窗口自己的 page(argv[2]) / limit(argv[3])，12 个窗口各扫
     state 1 的一页，已付款队列超过 24 条时第 2 页以后没人看得见；本进程翻页扫全量。
  2) 窗口一轮最长 1200 秒还叠加退避，接单延迟会变成一小时。
  3) 状态 3（异常）没有任何进程回收，写出去就是死单，本进程补这个回环。

所有 HTTP 调用都是显式超时 + 有限重试，绝不复制 function.py 里的 while True 死循环：
一条坏单不能把单进程守护堵死。
"""

import atexit
import ctypes
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

import order_baseline as B

BASE = os.path.dirname(os.path.abspath(__file__))
GOODS = [g.strip() for g in os.environ.get('SHARE_GOODS', '1763,643').split(',') if g.strip()]
CYCLE = float(os.environ.get('SHARE_INTAKE_CYCLE', 20))
PAGE_LIMIT = int(os.environ.get('SHARE_INTAKE_PAGE_LIMIT', 50))
MAX_PAGES = int(os.environ.get('SHARE_INTAKE_PAGES', 20))
TIMEOUT = float(os.environ.get('SHARE_INTAKE_TIMEOUT', 15))
LOSE_NUM = int(os.environ.get('SHARE_LOSE_NUM', 5))
REVIVE_MAX = int(os.environ.get('SHARE_REVIVE_MAX', 3))
CONFIRM_RETRY = int(os.environ.get('SHARE_CONFIRM_RETRY', 3))
CONFIRM_GAP = float(os.environ.get('SHARE_CONFIRM_GAP', 2))
ONCE = str(os.environ.get('SHARE_INTAKE_ONCE', '0')).lower() not in ('0', 'false', 'no', 'off')
DRY = str(os.environ.get('SHARE_INTAKE_DRY', '0')).lower() not in ('0', 'false', 'no', 'off')
STATE_FILE = os.path.join(BASE, 'intake_state.json')
LOCK_FILE = os.path.join(BASE, 'intake.lock')


def show(tag, msg, content=''):
    print('{} | {} | {} | {}'.format(time.strftime('%H:%M:%S'), tag, msg, content), flush=True)


# ---------------------------------------------------------------- 单实例锁
def _pid_alive(pid):
    """Windows 上不能用 os.kill(pid, 0) 判活：CPython 会走 TerminateProcess 把人杀掉。"""
    if pid <= 0:
        return False
    try:
        k32 = ctypes.windll.kernel32
    except Exception:
        return False
    handle = k32.OpenProcess(0x1000, False, int(pid))     # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = ctypes.c_ulong(0)
    ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
    k32.CloseHandle(handle)
    return bool(ok) and code.value == 259                 # STILL_ACTIVE


def hold_lock():
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, 'r', encoding='utf-8') as fh:
                pid = int((fh.read() or '0').split()[0])
        except Exception:
            pid = 0
        if _pid_alive(pid):
            show('守护', '已在运行', 'pid={} 不重复启动'.format(pid))
            return False
    with open(LOCK_FILE, 'w', encoding='utf-8') as fh:
        fh.write('{} {}'.format(os.getpid(), int(time.time())))
    return True


def release_lock():
    try:
        os.remove(LOCK_FILE)
    except Exception:
        pass


# ---------------------------------------------------------------- 状态文件
def load_state():
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def save_state(state):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def prune(state):
    """七天没被碰过的单子从回收账本里清掉，免得文件越滚越大。"""
    edge = time.time() - 7 * 86400
    for sn in [k for k, v in state.items() if float((v or {}).get('ts') or 0) < edge]:
        state.pop(sn, None)


# ---------------------------------------------------------------- 平台接口
def load_cfg(goods):
    path = os.path.join(BASE, 'cfg_qw_dy_share_{}.json'.format(goods))
    with open(path, 'r', encoding='utf-8') as fh:
        cfg = json.load(fh)
    cfg['goods'] = str(goods)
    return cfg


class Api(object):
    """最小客户端：只带 AppId / AppToken / AppTimestamp，签名算法与 function.py 一致。"""

    def __init__(self, cfg):
        self.api_url = cfg['api_url']
        self.app_id = cfg['app_id']
        self.app_secret = cfg['app_secret']
        self.three_url = cfg['three_url']
        self.goods = cfg['goods']

    def _headers(self, query_url, is_json=False):
        ts = str(int(time.time()))
        token = hashlib.sha1(
            '{}{}{}{}'.format(self.app_id, self.app_secret, query_url, ts).encode('utf-8')).hexdigest()
        head = {'AppId': self.app_id, 'AppToken': token, 'AppTimestamp': ts}
        if is_json:
            head['Content-Type'] = 'application/json'
        return head

    def _open(self, req):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.getcode(), resp.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read().decode('utf-8', 'replace')
            except Exception:
                return exc.code, ''
        except Exception as exc:
            return -1, str(exc)[:160]

    def _post(self, query_url, payload=None):
        body = None
        head = self._headers(query_url, payload is not None)
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request('{}{}'.format(self.api_url, query_url), data=body,
                                     headers=head, method='POST')
        return self._open(req)

    def orders(self, state, page=1):
        """返回本页订单列表；接口异常返回 None（与「本页为空」区分开）。"""
        query = ('/api/supplier/order/v2/orders?page={}&goodsSN={}&orderSN=&state={}&limit={}'
                 .format(page, self.goods, state, PAGE_LIMIT))
        code, text = self._post(query)
        if code != 200:
            show('接口', '订单列表失败', '货号 {} state {} page {} | {} {}'.format(
                self.goods, state, page, code, text[:100]))
            return None
        try:
            body = json.loads(text)
        except Exception:
            show('接口', '订单列表非 JSON', text[:120])
            return None
        if not isinstance(body, dict):
            return None
        if int(body.get('code', 0)) == 403:
            show('接口', '鉴权失败 403', '检查 app_id / app_secret')
            return None
        return ((body.get('result') or {}).get('data') or [])

    def detail(self, order_sn):
        query = '/api/supplier/order/v2/orders?orderSN={}'.format(order_sn)
        code, text = self._post(query)
        if code != 200:
            return {}
        try:
            body = json.loads(text)
        except Exception:
            return {}
        data = ((body.get('result') or {}).get('data') or {})
        if isinstance(data, list):
            return data[0] if data else {}
        return data or {}

    def set_state(self, order_sn, state, remark):
        if DRY:
            show(order_sn, '演练置状态', 'state={} {}'.format(state, remark))
            return 'dry'
        code, text = self._post('/api/supplier/order/v2/order',
                                {'orderSN': order_sn, 'state': int(state), 'remarks': remark})
        return '{} {}'.format(code, text[:80])

    def exec_num(self, order_sn):
        """三方接口回已执行量（裸标量）；失败返回 None，不要当成 0。"""
        req = urllib.request.Request('{}{}'.format(self.three_url, 'execNum?order_sn={}'.format(order_sn)),
                                     method='GET')
        code, text = self._open(req)
        if code != 200:
            return None
        try:
            return int(float(str(text).strip()))
        except Exception:
            return None


def sweep(app, state):
    """翻页扫完某种货在某个状态下的全部订单，不再依赖窗口的 page/limit。"""
    out = []
    for page in range(1, MAX_PAGES + 1):
        rows = app.orders(state, page)
        if rows is None:
            break                     # 接口异常：本状态本轮放弃，别把半截列表当全量
        if not rows:
            break
        out.extend(rows)
        if len(rows) < PAGE_LIMIT:
            break                     # 不够一页就是最后一页，省一次空请求
    return out


def _locked(order_sn):
    return os.path.exists(os.path.join(BASE, 'order_{}.lock'.format(order_sn)))


# ---------------------------------------------------------------- 三个作业
def job_accept(apps, state):
    """已付款(1) → 处理中(2)。客户付完款最迟一个周期开工。"""
    done = 0
    for app in apps:
        for order in sweep(app, '1'):
            sn = str(order.get('orderSN') or '')
            if not sn:
                continue
            msg = app.set_state(sn, 2, '自动接单')
            done += 1
            show(sn, '自动接单', '货号 {} | 下单 {} 条 | {}'.format(
                app.goods, order.get('number'), msg))
    return done


def job_complete(apps, state):
    """处理中(2) → 已完成(4)，写后回读复核。

    为什么要兜底这一层：窗口写 4 的分支要求它自己把量刷到位（缺量<=LOSE_NUM），
    进程被关、单子被抢锁、窗口崩溃都会让「其实已经刷满」的单卡在 2，
    客户永远看不到已完成。这里按订单详情判一次，与窗口判的是同一件事。

    进度口径必须是详情里的 currentNum / startNum：
      已交付 = currentNum - startNum，缺量 = number - 已交付。
    实测三方 execNum 接口对「重新放行过的单」回 0（3246294 实际已交付 148 也回 0），
    拿它判完成会把单子一直吊在处理中，所以这里只用详情字段。
    """
    done = 0
    for app in apps:
        for order in sweep(app, '2'):
            sn = str(order.get('orderSN') or '')
            if not sn:
                continue
            if _locked(sn):
                continue              # 有窗口正在跑这单，别抢在它收口之前判完成
            detail = app.detail(sn) or {}
            if str(detail.get('state') or '') != '2':
                continue              # 详情状态已变（被退款/被判异常），本轮不碰
            try:
                number = int(detail.get('number') or order.get('number') or 0)
                start = int(detail.get('startNum') or order.get('startNum') or 0)
                current = int(detail.get('currentNum') or 0)
            except Exception:
                continue
            if number <= 0:
                continue
            # 平台不下发 startNum（实测恒为 0）时，remain = number - currentNum 会把
            # 「视频历史总分享量」当成已交付，100 条的小单会被这里直接判完成。
            # 所以 start<=0 一律以本地基线为准；连基线都没有就交给窗口去建，不在守护里盲判。
            if start <= 0:
                start = B.get(sn)
                if start <= 0:
                    continue
                if current <= 0:
                    continue          # 一个执行量都没回写过，谈不上完成
            remain = number - (current - start)
            if remain > LOSE_NUM:
                continue
            last = ''
            for attempt in range(1, CONFIRM_RETRY + 1):
                app.set_state(sn, 4, '订单完成')
                time.sleep(CONFIRM_GAP)
                last = str(app.detail(sn).get('state') or '')
                if last == '4':
                    done += 1
                    show(sn, '完成确认', '缺量 {} | 第 {} 次写入生效'.format(remain, attempt))
                    break
            else:
                show(sn, '完成确认失败', '写入 {} 次仍是 state={}'.format(CONFIRM_RETRY, last))
    return done


def job_revive(apps, state):
    """异常(3) → 处理中(2)，同一单最多回收 REVIVE_MAX 次。

    两处会写 3：aweme_id 解析失败、分享量读取失败。前者多半是链接本身的问题，
    所以给次数上限而不是无限循环；后者是临时故障，回收后基本自愈。
    """
    done = 0
    for app in apps:
        for order in sweep(app, '3'):
            sn = str(order.get('orderSN') or '')
            if not sn:
                continue
            if _locked(sn):
                continue
            rec = state.setdefault(sn, {'revive': 0, 'ts': 0})
            used = int(rec.get('revive') or 0)
            if used >= REVIVE_MAX:
                continue
            rec['revive'] = used + 1
            rec['ts'] = int(time.time())
            app.set_state(sn, 2, '自动回收第{}次'.format(used + 1))
            time.sleep(CONFIRM_GAP)
            now_state = str(app.detail(sn).get('state') or '')
            if now_state == '2':
                done += 1
                show(sn, '异常回收', '第 {}/{} 次，已回到处理中'.format(used + 1, REVIVE_MAX))
            else:
                show(sn, '异常回收失败', '第 {}/{} 次后仍是 state={}'.format(
                    used + 1, REVIVE_MAX, now_state))
    return done


# ---------------------------------------------------------------- 主循环
def main():
    if not hold_lock():
        return 3                  # 3 给启动脚本识别「已有实例」，别在重启循环里空转
    atexit.register(release_lock)
    apps = []
    for goods in GOODS:
        try:
            apps.append(Api(load_cfg(goods)))
        except Exception as exc:
            show('守护', '配置读取失败', '货号 {} | {}'.format(goods, exc))
    if not apps:
        show('守护', '没有可用配置', '检查 cfg_qw_dy_share_<货号>.json')
        return 1
    state = load_state()
    show('守护', '启动', '货号 {} | 周期 {}s | 页宽 {} | 干跑 {} | 单圈 {}'.format(
        ','.join(GOODS), CYCLE, PAGE_LIMIT, DRY, ONCE))
    loop = 0
    while True:
        loop += 1
        began = time.time()
        try:
            accepted = job_accept(apps, state)
            finished = job_complete(apps, state)
            revived = job_revive(apps, state)
        except Exception as exc:
            show('守护', '本轮异常', str(exc)[:160])
            accepted = finished = revived = 0
        prune(state)
        try:
            save_state(state)
        except Exception as exc:
            show('守护', '状态写盘失败', str(exc)[:120])
        cost = time.time() - began
        show('守护', '第 {} 圈'.format(loop),
             '接单 {} | 完成 {} | 回收 {} | 用时 {:.1f}s'.format(
                 accepted, finished, revived, cost))
        if ONCE:
            break
        time.sleep(max(5.0, CYCLE - cost))
    return 0


if __name__ == '__main__':
    sys.exit(main() or 0)
