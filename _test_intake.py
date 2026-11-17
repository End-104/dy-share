# -*- coding: utf-8 -*-
"""本地假平台，验证接单 / 完成 / 回收 三条逻辑（不连真接口）。"""
import os
import sys

sys.path.insert(0, r'D:\dy_share')
import dy_share_intake as I


class FakeApi(object):
    """最小假平台：orders 支持翻页，detail / set_state 有状态机。"""

    def __init__(self, goods='1763', orders=None, reject_state=None):
        self.goods = goods
        self.rows = orders or []          # list of dict
        self.reject_state = reject_state  # {sn: 拒绝写入的状态} 用来测确认失败分支
        self.writes = []                  # (sn, state, remark)

    def orders(self, state, page=1):
        same = [r for r in self.rows if str(r.get('state')) == str(state)]
        size = I.PAGE_LIMIT
        return same[(page - 1) * size: page * size]

    def detail(self, order_sn):
        for r in self.rows:
            if str(r.get('orderSN')) == str(order_sn):
                return dict(r)
        return {}

    def set_state(self, order_sn, state, remark):
        self.writes.append((order_sn, int(state), remark))
        for r in self.rows:
            if str(r.get('orderSN')) != str(order_sn):
                continue
            if self.reject_state and self.reject_state.get(str(order_sn)) == int(state):
                return '200 {"code":900}'
            r['state'] = int(state)
            return '200 {"code":100,"msg":"ok"}'
        return '200 {"code":900}'

    def exec_num(self, order_sn):
        return 0


def row(sn, number, start, current, state):
    return {'orderSN': sn, 'number': str(number), 'startNum': start,
            'currentNum': current, 'state': state}


ok = True


def check(name, cond, extra=''):
    global ok
    ok = ok and bool(cond)
    print(('PASS ' if cond else 'FAIL ') + name + ((' | ' + str(extra)) if extra else ''))


# ---------------- 1 接单：state 1 -> 2
api = FakeApi(orders=[row(1001, 500, 0, 0, 1), row(1002, 800, 0, 0, 1), row(1003, 500, 0, 0, 2)])
n = I.job_accept([api], {})
check('接单数量=2', n == 2, n)
check('1001 变处理中', api.detail(1001)['state'] == 2)
check('1003 未被重复接单', all(w[0] != 1003 for w in api.writes))

# ---------------- 2 完成：缺量 4 应写 4 并复核通过
api = FakeApi(orders=[row(2001, 1000, 0, 996, 2), row(2002, 1000, 0, 900, 2)])
n = I.job_complete([api], {})
check('完成数量=1（只判满的那单）', n == 1, n)
check('2001 变已完成', api.detail(2001)['state'] == 4)
check('2002 仍处理中', api.detail(2002)['state'] == 2)

# ---------------- 3 完成：startNum 偏移必须扣掉
api = FakeApi(orders=[row(2101, 1000, 966, 1114, 2)])   # 已交付 148，缺 852
n = I.job_complete([api], {})
check('startNum 偏移：缺 852 不判完成', n == 0 and api.detail(2101)['state'] == 2)
api = FakeApi(orders=[row(2102, 1000, 966, 1963, 2)])   # 已交付 997，缺 3
n = I.job_complete([api], {})
check('startNum 偏移：缺 3 判完成', n == 1 and api.detail(2102)['state'] == 4)

# ---------------- 4 完成：平台吞状态时重试 3 次后放弃
api = FakeApi(orders=[row(2201, 500, 0, 500, 2)], reject_state={'2201': 4})
n = I.job_complete([api], {})
writes_4 = [w for w in api.writes if w[1] == 4 and str(w[0]) == '2201']
check('确认失败时写 4 三次', len(writes_4) == I.CONFIRM_RETRY, len(writes_4))
check('确认失败不计完成', n == 0 and api.detail(2201)['state'] == 2)

# ---------------- 5 完成：有窗口锁在跑这单时跳过
lock_path = os.path.join(I.BASE, 'order_2301.lock')
with open(lock_path, 'w', encoding='utf-8') as fh:
    fh.write('{} 0'.format(os.getpid()))
try:
    api = FakeApi(orders=[row(2301, 500, 0, 500, 2)])
    n = I.job_complete([api], {})
    check('锁占用时跳过完成', n == 0 and api.detail(2301)['state'] == 2)
finally:
    os.remove(lock_path)

# ---------------- 6 回收：3 -> 2，次数到顶后不再动
state = {}
api = FakeApi(orders=[row(3001, 500, 0, 0, 3)])
for expect in (1, 2, 3):
    api.rows[0]['state'] = 3
    n = I.job_revive([api], state)
    check('回收第 {} 次成功'.format(expect), n == 1 and api.detail(3001)['state'] == 2,
          state.get('3001'))
api.rows[0]['state'] = 3
n = I.job_revive([api], state)
check('回收达上限后不动', n == 0 and api.detail(3001)['state'] == 3, state.get('3001'))

# ---------------- 7 回收：平台不认回收时留在 3
state = {}
api = FakeApi(orders=[row(3101, 500, 0, 0, 3)], reject_state={'3101': 2})
n = I.job_revive([api], state)
check('回收被拒不计成功', n == 0 and api.detail(3101)['state'] == 3)
check('回收被拒仍记一次', state.get('3101', {}).get('revive') == 1, state.get('3101'))

# ---------------- 8 翻页：120 条 state 2 必须全扫到
api = FakeApi(orders=[row(4000 + i, 500, 0, 0, 2) for i in range(120)])
rows = I.sweep(api, '2')
check('翻页扫全 120 条', len(rows) == 120, len(rows))

# ---------------- 9 干跑：真实 Api.set_state 必须短路，不发请求
I.DRY = True
try:
    probe = I.Api({'api_url': 'http://127.0.0.1:1', 'app_id': 'x', 'app_secret': 'y',
                   'three_url': 'http://127.0.0.1:1/', 'goods': '1763'})
    got = probe.set_state(5001, 4, '干跑')
    check('干跑时真实接口层短路', got == 'dry', got)
finally:
    I.DRY = False
probe = I.Api({'api_url': 'http://127.0.0.1:1', 'app_id': 'x', 'app_secret': 'y',
               'three_url': 'http://127.0.0.1:1/', 'goods': '1763'})
got = probe.set_state(5001, 4, '连不通的地址')
check('非干跑时会真的发请求（失败也能返回不抛异常）', got.startswith('-1') or got.startswith('4'), got)

# ---------------- 10 接口异常时 sweep 不返回半截列表
class DeadApi(FakeApi):
    def orders(self, state, page=1):
        if page == 2:
            return None
        return FakeApi.orders(self, state, page)


api = DeadApi(orders=[row(6000 + i, 500, 0, 0, 2) for i in range(120)])
rows = I.sweep(api, '2')
check('中途接口异常只保留第一页（不误判全量）', len(rows) == I.PAGE_LIMIT, len(rows))

print('ALL_PASS' if ok else 'HAS_FAIL')
