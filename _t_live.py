# -*- coding: utf-8 -*-
"""只读：把两个货号 state=4（完成）与 state=5 的单全拉下来，看它们到底是
「真的交付够了才关」还是「零交付就被关」。只读查询。"""
import os, json, time, hashlib
from datetime import datetime
import httpx

os.chdir(os.path.dirname(os.path.abspath(__file__)))
CLIENT = httpx.Client(timeout=15.0, verify=False, follow_redirects=True)

def load_cfg(name):
    with open(name + '.json', 'r', encoding='utf-8') as f:
        return json.load(f)

def headers(cfg, q):
    ts = str(int(time.time()))
    tok = hashlib.sha1(f"{cfg['app_id']}{cfg['app_secret']}{q}{ts}".encode()).hexdigest()
    return {'AppId': cfg['app_id'], 'AppToken': tok, 'AppTimestamp': ts}

def get_list(cfg, state, goods, page=1, limit=50):
    q = f'/api/supplier/order/v2/orders?page={page}&goodsSN={goods}&orderSN=&state={state}&limit={limit}'
    r = CLIENT.post(cfg['api_url'] + q, headers=headers(cfg, q))
    try:
        return json.loads(r.text)
    except Exception:
        return {}

def collect(cfg, goods, state):
    out, tot = {}, None
    for p in range(1, 41):
        b = get_list(cfg, state, goods, p, 50)
        res = b.get('result') or {}
        if tot is None:
            tot = res.get('total')
        data = res.get('data') or []
        if not data:
            break
        for o in data:
            out[str(o.get('orderSN'))] = o
        if len(data) < 50:
            break
    return out, tot

def iv(o, k):
    try:
        return int(o.get(k) or 0)
    except Exception:
        return 0

def ts_of(v):
    try:
        return datetime.fromtimestamp(int(v)).strftime('%m-%d %H:%M')
    except Exception:
        return '?'

print('状态构成核查:', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
print('=' * 86)
for cname in ('cfg_qw_dy_share_1594', 'cfg_qw_dy_share_997'):
    cfg = load_cfg(cname)
    g = cfg['goods_sn']
    for st in ('1', '2', '3', '4', '5'):
        m, tot = collect(cfg, g, st)
        if not m:
            print(f'goods={g} state={st}: total={tot} 空')
            continue
        tgt = sum(iv(o, 'number') for o in m.values())
        raw = sum(iv(o, 'currentNum') for o in m.values())
        base = sum(iv(o, 'startNum') for o in m.values())
        deliv = sum(max(0, iv(o, 'currentNum') - iv(o, 'startNum')) for o in m.values())
        zero = sum(1 for o in m.values() if max(0, iv(o, 'currentNum') - iv(o, 'startNum')) == 0)
        print(f'goods={g} state={st}: total={tot} 拉到 {len(m)} 单 | 目标 {tgt} 条 | '
              f'currentNum 合计 {raw} | startNum 合计 {base} | 实际交付 {deliv} 条 | 零交付 {zero} 单')
        if st in ('4', '5') and m:
            print(f'   最晚 12 单（按创建时间倒序）:')
            rows = sorted(m.items(), key=lambda kv: -(iv(kv[1], 'createdAt')))
            for sn, o in rows[:12]:
                c, s, n = iv(o, 'currentNum'), iv(o, 'startNum'), iv(o, 'number')
                print(f'     {sn}  目标 {n:>5}  currentNum {c:>6}  startNum {s:>6}  '
                      f'实际 {max(0,c-s):>5}  {max(0,c-s)/n*100 if n else 0:>6.1f}%  '
                      f'建于 {ts_of(o.get("createdAt"))}')
print('\n完成:', datetime.now().strftime('%H:%M:%S'))